#!/usr/bin/env bash
# Суточный дамп базы стенда (DEPLOY.md §8), хранится 7 дней на самом
# сервере. Стенд — без клиентских данных, поэтому копии вне хоста здесь
# нет; для боя она обязательна. Запускает cron (bootstrap.sh).
set -euo pipefail
# shellcheck source=/dev/null
. /etc/corp-ed/stage.env

# В дампе — документы и данные пользователей: файл только для владельца.
umask 077
dir=/var/backups/corp-ed
file="$dir/corp_ed-$(date +%F).dump"
cd "$APP_DIR"
docker compose -f compose.yaml exec -T db pg_dump -U corp_ed -Fc corp_ed > "$file.tmp"
mv "$file.tmp" "$file"
find "$dir" -name 'corp_ed-*.dump' -mtime +7 -delete
echo "$(date -u +%FT%TZ) backup $(du -h "$file" | cut -f1) $file"
