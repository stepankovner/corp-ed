#!/usr/bin/env bash
# Суточный дамп базы стенда (DEPLOY.md §8). На сервере — только последний,
# когда настроена копия вне сервера (решение 09.10: дамп на сервере не
# шифруется, поэтому хранится минимум); без неё — 7 дней. Запускает cron
# (bootstrap.sh). Сразу после дампа restore-check.sh поднимает его во
# временной базе, затем offsite.sh отправляет зашифрованную копию в
# хранилище вне сервера (если настроена: на стенде данные тестовые, для
# боя копия обязательна). Дамп, который не
# восстанавливается или не ушёл, — неудача задачи, мониторинг тревожит.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/kronto/stage.env

# В дампе — документы и данные пользователей: файл только для владельца.
umask 077
dir=/var/backups/kronto
file="$dir/kronto-$(date +%F).dump"
cd "$APP_DIR"
docker compose -f compose.yaml exec -T db pg_dump -U corp_ed -Fc corp_ed > "$file.tmp"
mv "$file.tmp" "$file"
find "$dir" -name 'kronto-*.dump' -mtime +7 -delete
echo "$(date -u +%FT%TZ) backup $(du -h "$file" | cut -f1) $file"
# Проверка после выкатки (restore-check.sh report) снимает дамп сама и
# проверяет его под своей блокировкой.
# Копию вне сервера отправляет и она же (offsite.sh report).
if [[ -z "${KRONTO_BACKUP_NO_VERIFY:-}" ]]; then
    "$APP_DIR/deploy/stage/restore-check.sh" run "$file"
    "$APP_DIR/deploy/stage/offsite.sh" upload "$file"
    # Дамп проверен и ушёл вне сервера — прежние больше не нужны.
    if [[ -r "${KRONTO_OFFSITE_ENV:-/etc/kronto/offsite.env}" ]]; then
        find "$dir" -name 'kronto-*.dump' ! -samefile "$file" -delete
    fi
fi
