#!/usr/bin/env bash
# Выкатка коммита на тестовый стенд (docs/STAGE.md). Образы собрал
# workflow Deploy по этому коммиту и положил в GHCR; здесь они только
# скачиваются и получают локальные имена из compose.yaml — поэтому
# compose.yaml на стенде тот же, что в разработке и в бою.
#
# Вызывает kronto-deploy (ssh-entry.sh) после checkout коммита. Откат —
# Run workflow «Deploy» с полем sha; с сервера (образ из GHCR — если пакет
# публичный или сделан docker login):
#   sudo -u deploy SSH_ORIGINAL_COMMAND="deploy <sha>" /usr/local/bin/kronto-deploy
set -euo pipefail
# shellcheck source=/dev/null
. /etc/kronto/stage.env

sha="${1:?sha коммита}"
cd "$APP_DIR"
compose=(docker compose -f compose.yaml)

[[ -f .env ]] || { echo ".env не найден: сначала bootstrap.sh" >&2; exit 1; }

# Значение строки KEY=… без пробелов и комментария; нет строки — пусто и код 1.
env_value() { awk -v k="$1" -F= '$1 == k { sub(/^[^=]*=/, ""); sub(/[ \t]*#.*$/, ""); v = $0; f = 1 } END { print v; exit !f }' "$2"; }
# Заменить все строки KEY=… на KEY=<значение> как есть. Не sed: «|», «&» и
# «\» в значении сломали бы выражение замены. Значение — через окружение
# awk (ENVIRON), а не -v: -v разбирает обратные слэши.
set_env_value() {
    local tmp
    tmp=$(mktemp .env.XXXXXX)
    if ! KEY="$1" VALUE="$2" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"] }
        index($0, k "=") == 1 { print k "=" v; next } { print }' .env >"$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    chmod --reference=.env "$tmp"
    mv "$tmp" .env
}

# Стенд работает в боевом режиме (make-env.sh). Без него молча выключаются
# HSTS, запрет «*» в ALLOWED_HOSTS и CORS, запрет LLM_PROVIDER=fake и
# скрытие /docs — выкатку останавливаем до скачивания образов.
environment=$(env_value ENVIRONMENT .env || true)
environment=${environment//[\"\']/}
if [[ "$environment" != production ]]; then
    echo ".env: ENVIRONMENT=${environment:-(нет)}, нужно production — выкатка остановлена" >&2
    exit 1
fi

echo "==> образы $sha"
# Токен GHCR из workflow (stdin, живёт до конца его job): пакеты могут
# оставаться приватными. Вход — во временный конфиг Docker, чтобы токен
# не остался на диске. Без токена (ручной запуск из терминала) — обычный
# docker pull: публичный пакет или свой docker login.
token=""
if [[ ! -t 0 ]]; then
    IFS= read -r token || true
fi
if [[ -n "$token" ]]; then
    docker_config=$(mktemp -d)
    trap 'rm -rf "$docker_config"' EXIT
    export DOCKER_CONFIG="$docker_config"
    printf '%s' "$token" | docker login "${IMAGE_PREFIX%%/*}" -u deploy --password-stdin >/dev/null
fi
for name in kronto-api kronto-web; do
    docker pull --quiet "$IMAGE_PREFIX/$name:$sha" >/dev/null
    docker tag "$IMAGE_PREFIX/$name:$sha" "$name:local"
done
if [[ -n "$token" ]]; then
    docker logout "${IMAGE_PREFIX%%/*}" >/dev/null
    unset DOCKER_CONFIG
fi

echo "==> настройки ответов"
# Решения ML и владельца о том, как отвечать (порог, BH-37, память
# диалога…), едут с кодом: значение — из .env.example этого коммита, каждое
# изменение — в лог выкатки. Не трогаем то, чему нужна переиндексация
# (RAG_CHUNK_TOKENS, RAG_OVERLAP_TOKENS) или ручной шаг на сервере
# (RAG_RERANK_MODEL — модель и профиль compose, STAGE.md §5).
answer_keys=(
    RAG_FAQ_LIMIT RAG_FAQ_MAX_DISTANCE RAG_FAQ_GATE_DISTANCE RAG_FAQ_NEAR_MARGIN
    RAG_CONTEXT_MAX_TOKENS RAG_FAQ_TEMPERATURE RAG_RETRIEVER RAG_FULLTEXT_WEIGHT
    RAG_HISTORY_TURNS RAG_HISTORY_TTL_MINUTES RAG_CONDENSE_TIMEOUT_SECONDS
    RAG_RERANK_MAX_WORDS
)
for key in "${answer_keys[@]}"; do
    want=$(env_value "$key" .env.example) || continue
    if have=$(env_value "$key" .env); then
        [[ "$have" == "$want" ]] && continue
        set_env_value "$key" "$want"
    else
        have="(нет)"
        printf '%s=%s\n' "$key" "$want" >> .env
    fi
    echo "    $key: ${have:-(пусто)} → ${want:-(пусто)}"
done

echo "==> compose up"
# up ждёт migrate (service_completed_successfully): упавшая миграция
# останавливает выкатку здесь, старые api и worker продолжают работать.
"${compose[@]}" up -d --no-build --remove-orphans

echo "==> ждём healthy"
deadline=$((SECONDS + 300))
for svc in api worker web; do
    id=$("${compose[@]}" ps -q "$svc")
    until [[ "$(docker inspect -f '{{.State.Health.Status}}' "$id")" == healthy ]]; do
        if ((SECONDS > deadline)); then
            echo "$svc не стал healthy за 5 минут" >&2
            "${compose[@]}" ps >&2
            "${compose[@]}" logs --tail 80 "$svc" >&2
            exit 1
        fi
        sleep 5
    done
done

echo "==> боевой режим"
# Приложение действительно в production: схема API закрыта. Напрямую в
# api (порт на 127.0.0.1, он есть в ALLOWED_HOSTS), мимо HTTPS_PROXY.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 --noproxy '*' \
    http://127.0.0.1:8000/openapi.json || true)
if [[ "$code" != 404 ]]; then
    echo "api отдал /openapi.json с кодом $code, а не 404: не боевой режим?" >&2
    exit 1
fi

echo "==> песочница сайта"
# Вымышленная компания для /demo (ТЗ §1): заводится один раз, дальше —
# только изменённые документы из src/corp_ed/demo. Идемпотентно.
"${compose[@]}" run --rm --no-deps -T api python -m corp_ed.cli demo setup

echo "==> проверка через nginx"
# Тот же путь, что у пользователя: TLS, прокси, TrustedHost. Мимо
# HTTPS_PROXY, если он есть в окружении (DEPLOY.md §9a): --resolve его
# не обходит, и проверка ушла бы наружу.
local_https=(curl -fsS --max-time 10 --noproxy '*' --resolve "$DOMAIN:443:127.0.0.1")
"${local_https[@]}" "https://$DOMAIN/health" >/dev/null
# Главная — готовый HTML сайта (предрендер, ТЗ §1), а не пустая оболочка.
home=$("${local_https[@]}" "https://$DOMAIN/")
grep -q 'data-prerender' <<<"$home" || { echo "главная без готового HTML" >&2; exit 1; }

# Мониторинг (deploy/monitoring, П-9) — из того же коммита, что приложение:
# правила тревог и дашборд едут вместе с кодом. Его сбой выкатку не
# останавливает — приложение уже работает, — но виден в логе workflow.
if [[ -f /etc/kronto/monitoring.env ]]; then
    echo "==> мониторинг"
    if ! docker compose -f deploy/monitoring/compose.yaml \
        --env-file .env --env-file /etc/kronto/monitoring.env \
        up -d --remove-orphans --quiet-pull; then
        echo "ВНИМАНИЕ: мониторинг не поднялся (приложение выкачено)" >&2
    fi
fi

printf '%s %s\n' "$(date -u +%FT%TZ)" "$sha" >> /var/log/kronto/deploys.log
# Неиспользуемые образы старше недели: иначе каждая выкатка оставляет
# ~1 ГБ. Работающие контейнеры prune не трогает; для отката старый образ
# скачается из GHCR заново.
docker image prune -af --filter "until=168h" >/dev/null
echo "==> выкачен $sha"
