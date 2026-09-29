#!/usr/bin/env bash
# .env тестового стенда из .env.example: боевой режим, случайные секреты,
# адреса стенда. Значения ML (RAG_*, GAPS_*) и комментарии берутся из
# шаблона как есть. Ключи Yandex Cloud скрипт не знает — их вписывают
# руками. Пишет в stdout; bootstrap.sh кладёт результат в .env (600).
#
#   DOMAIN=stage.krontoai.ru bash deploy/stage/make-env.sh .env.example > .env
set -euo pipefail

: "${DOMAIN:?DOMAIN — имя стенда}"
template="${1:?путь к .env.example}"

hex() { openssl rand -hex "$1"; }
# Ключ Fernet — 32 случайных байта в urlsafe base64, как Fernet.generate_key().
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }

env_file=$(mktemp)
trap 'rm -f "$env_file"' EXIT
cp "$template" "$env_file"

# KEY=value: заменить строку «KEY=…» или «# KEY=…», иначе дописать в конец.
# Значения — hex, base64url, имя хоста и https-адреса: разделитель | в них
# не встречается.
set_var() {
    if grep -qE "^#? ?$1=" "$env_file"; then
        sed -i -E "s|^#? ?$1=.*|$1=$2|" "$env_file"
    else
        printf '%s=%s\n' "$1" "$2" >> "$env_file"
    fi
}

set_var ENVIRONMENT production
set_var DEBUG false
# Пароли — hex: без символов, которые пришлось бы экранировать в DSN.
set_var POSTGRES_PASSWORD "$(hex 24)"
set_var APP_DB_PASSWORD "$(hex 24)"
set_var REDIS_PASSWORD "$(hex 24)"
set_var SECRET_KEY "$(hex 32)"
set_var CONNECTOR_SECRETS_KEYS "$(fernet)"
# 127.0.0.1 — HEALTHCHECK контейнера идёт через ту же проверку Host.
set_var ALLOWED_HOSTS "$DOMAIN,127.0.0.1"
set_var CONNECTOR_OAUTH_CALLBACK_URL "https://$DOMAIN/api/v1/connectors/oauth/callback"
set_var CONNECTOR_OAUTH_RETURN_URL "https://$DOMAIN/sources"
# Заглушки шаблона не должны выглядеть как настоящие ключи.
set_var YC_FOLDER_ID ""
set_var YC_API_KEY ""
# Строки шаблона для запуска без Docker: в compose DATABASE_URL задаёт сам
# compose.yaml, тестовая база на стенде не нужна.
sed -i -E '/^(DATABASE_URL|TEST_DATABASE_URL)=/d' "$env_file"

cat "$env_file"
