#!/usr/bin/env bash
# Выкатка коммита на тестовый стенд (docs/STAGE.md). Образы собрал
# workflow Deploy по этому коммиту и положил в GHCR; здесь они только
# скачиваются и получают локальные имена из compose.yaml — поэтому
# compose.yaml на стенде тот же, что в разработке и в бою.
#
# Вызывает corp-ed-deploy (ssh-entry.sh) после checkout коммита. Вручную
# (откат — то же самое со старым sha):
#   sudo -u deploy SSH_ORIGINAL_COMMAND="deploy <sha>" /usr/local/bin/corp-ed-deploy
set -euo pipefail
# shellcheck source=/dev/null
. /etc/corp-ed/stage.env

sha="${1:?sha коммита}"
cd "$APP_DIR"
compose=(docker compose -f compose.yaml)

[[ -f .env ]] || { echo ".env не найден: сначала bootstrap.sh" >&2; exit 1; }

echo "==> образы $sha"
for name in corp-ed kronto-web; do
    docker pull --quiet "$IMAGE_PREFIX/$name:$sha" >/dev/null
    docker tag "$IMAGE_PREFIX/$name:$sha" "$name:local"
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

echo "==> проверка через nginx"
# Тот же путь, что у пользователя: TLS, прокси, TrustedHost. Мимо
# HTTPS_PROXY, если он есть в окружении (DEPLOY.md §9a): --resolve его
# не обходит, и проверка ушла бы наружу.
local_https=(curl -fsS --max-time 10 --noproxy '*' --resolve "$DOMAIN:443:127.0.0.1")
"${local_https[@]}" "https://$DOMAIN/health" >/dev/null
"${local_https[@]}" -o /dev/null "https://$DOMAIN/"

printf '%s %s\n' "$(date -u +%FT%TZ)" "$sha" >> /var/log/corp-ed/deploys.log
# Неиспользуемые образы старше недели: иначе каждая выкатка оставляет
# ~1 ГБ. Работающие контейнеры prune не трогает; для отката старый образ
# скачается из GHCR заново.
docker image prune -af --filter "until=168h" >/dev/null
echo "==> выкачен $sha"
