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
"${local_https[@]}" -o /dev/null "https://$DOMAIN/"

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
