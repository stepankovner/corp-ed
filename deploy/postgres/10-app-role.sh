#!/bin/sh
# Роль приложения: только данные, без DDL и без обхода RLS.
#
# Выполняется образом postgres при первой инициализации тома
# (docker-entrypoint-initdb.d). Владелец схемы — POSTGRES_USER, под ним
# идут миграции. Приложение подключается как corp_ed_app: RLS для него
# действует (для суперпользователя — нет), таблицы он не создаёт и не
# удаляет. Права на будущие таблицы выдаются заранее через
# ALTER DEFAULT PRIVILEGES — новая миграция не требует ручных GRANT.
set -eu

: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v app_password="$APP_DB_PASSWORD" <<'SQL'
CREATE ROLE corp_ed_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO corp_ed_app;
GRANT USAGE ON SCHEMA public TO corp_ed_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO corp_ed_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO corp_ed_app;
SQL
