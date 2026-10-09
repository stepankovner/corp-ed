# Живой ownCloud Server 10 в Docker

Стенд для проверки вида `owncloud` на официальном образе
`owncloud/server`: Postgres, TLS-прокси nginx на 127.0.0.1 и та же
выдуманная компания, что у Nextcloud (`../nextcloud/seed.py`: `ivan`,
`maria`, `petr`, группа `hr`, папки и файл, расшаренные пользователю и
группе). Тесты — `tests/live/test_owncloud_live.py`, сами проверки общие
с Nextcloud (`../oc_family.py`); без переменных окружения тесты
пропускаются, в CI не идут.

Проверено 09.10.2026 на **ownCloud 10.16.6** (`owncloud/server:10.16.6`,
Postgres 16.15, nginx 1.31.6) — итоги внизу.

## Что нужно

- Docker, ~1,5 ГБ диска на образы, ~0,5 ГБ памяти.
- Postgres и Redis для тестов синхронизации — как у остального набора.
- При 429 от Docker Hub — зеркало `mirror.gcr.io/owncloud/server:10` и
  `docker tag` в имя из compose.yaml.

## Как поднять (≈2 минуты)

1. Сертификат стенда — как в `../nextcloud/README.md`, шаг 1
   (`DAV_TLS_DIR`, `DAV_STAND_CA`).

2. Запустить (порт 8445, другой — `OWNCLOUD_PORT`) и дождаться
   `"installed":true`:

   ```bash
   docker compose -f tests/live/owncloud/compose.yaml up -d
   curl -s --cacert ~/dav-tls/cert.pem https://127.0.0.1:8445/status.php
   ```

3. Засеять компанию (только на чистом стенде):

   ```bash
   OWNCLOUD_URL=https://127.0.0.1:8445/ uv run python -m tests.live.owncloud.seed
   ```

4. Тесты. Пароль приложения у ownCloud выдаёт только веб-интерфейс
   («Настройки → Безопасность → Новое приложение»): тесты входят формой
   и создают его из сессии (`seed.app_password`):

   ```bash
   OWNCLOUD_URL=https://127.0.0.1:8445/ \
     uv run pytest tests/live/test_owncloud_live.py -q --no-cov
   ```

5. `cli connector-check` — как в `../webdav/README.md`, шаг 5, с
   `--kind owncloud --config server=https://127.0.0.1:8445/`.

6. Убрать стенд: `docker compose -f tests/live/owncloud/compose.yaml down -v`.

Пароли пользователей — `Kronto-Test-1`, администратора — `Kronto-Admin-1`
(выдуманные, стенд только на 127.0.0.1).

## Что проверяют тесты

Те же семь, что у Nextcloud (таблица в `../nextcloud/README.md`): общий
файл — один документ (`owncloud:id:<fileid>`) на всех, кто его видит;
форматы; глубина 32/33; синхронизация трёх сотрудников с правкой,
удалением и снятой шарой; запись `connector-check` без паролей; неверный
пароль → `auth_failed` и гаснет только это подключение.

## Итоги 09.10.2026

- 7 из 7 тестов (≈1 минута).
- Корень `/remote.php/webdav/` отдаёт `oc:fileid`; ссылка
  `index.php/f/<fileid>`.
- Шары принимаются автоматически и лежат в корне дерева получателя под
  своим именем; файл, расшаренный и пользователю, и группе, — один.
- Новому пользователю ownCloud кладёт PDF-буклеты («Learn more about
  ownCloud»): они индексируются как личные файлы каждого.
- На стенде неверный пароль не замедлял следующие запросы (у Nextcloud —
  замедлял: защита от перебора).

## Что стенд не проверяет

- ownCloud Infinite Scale (oCIS): там другой WebDAV (пространства), этот
  вид его не читает (`connectors/webdav/kinds.py`).
- LDAP, вход по почте, внешние хранилища.
