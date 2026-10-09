# Живой WebDAV (сетевой диск, NAS) в Docker

Стенд для проверки вида `webdav` (`connectors/webdav/`) на настоящем
сервере WebDAV: Apache httpd 2.4 с mod_dav — типовой WebDAV-сервер
(как на многих NAS). Выдуманная компания: два
пользователя (`ivan`, `maria`), общее дерево «Общие» с вложенными
папками и файлами docx, pdf, md, txt и png, папка «Кадры» только для
`ivan`. Тесты — `tests/live/test_webdav_live.py`; без переменных
окружения они пропускаются, в CI не идут.

Проверено 09.10.2026 на **httpd 2.4.69** (Debian) — итоги внизу.

## Что нужно

- Docker, ~200 МБ диска на образ, памяти — десятки мегабайт.
- Postgres и Redis для тестов синхронизации — как у остального набора.

## Почему HTTPS на 127.0.0.1

Адаптер собирает адреса только с `https://` и не верит ссылкам (href) с
другой схемой, поэтому стенд — по HTTPS с самоподписанным сертификатом
на IP 127.0.0.1. Тесты доверяют только этому сертификату, а проверка
адреса (`core/outbound.py`) пропускает только этот стенд — всё остальное
идёт как в бою (`tests/live/dav_stand.py`).

## Как поднять (≈1 минута)

Все команды — из корня репозитория.

1. Сертификат стенда (вне репозитория):

   ```bash
   uv run python -m tests.live.dav_stand tls ~/dav-tls
   export DAV_TLS_DIR=~/dav-tls DAV_STAND_CA=~/dav-tls/cert.pem
   ```

2. Запустить Apache (порт 8443, другой — `WEBDAV_PORT`):

   ```bash
   docker compose -f tests/live/webdav/compose.yaml up -d
   ```

3. Засеять дерево (повторный запуск перезаписывает те же файлы):

   ```bash
   WEBDAV_URL=https://127.0.0.1:8443/dav/ uv run python -m tests.live.webdav.seed
   ```

4. Тесты:

   ```bash
   WEBDAV_URL=https://127.0.0.1:8443/dav/ \
     uv run pytest tests/live/test_webdav_live.py -q --no-cov
   ```

5. `cli connector-check` против стенда — тот же код CLI, только адрес
   стенда принимается и сертификату стенда доверяют. CLI читает общие
   настройки, поэтому нужны `SECRET_KEY` и `DATABASE_URL` (база не
   открывается — подойдут любые значения):

   ```bash
   SECRET_KEY=stand-only-0123456789abcdef0123456789 \
   DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/none \
     uv run python -m tests.live.dav_stand check \
       --stand https://127.0.0.1:8443 --ca ~/dav-tls/cert.pem -- \
       --kind webdav --config server=https://127.0.0.1:8443/dav/ \
       --credential login=maria --credential password=Kronto-Test-1 \
       --fetch 2 --record /tmp/webdav-record
   ```

6. Убрать стенд вместе с данными:

   ```bash
   docker compose -f tests/live/webdav/compose.yaml down -v
   ```

Пароль обоих пользователей — `Kronto-Test-1` (выдуманный, стенд только
на 127.0.0.1).

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `each_user_lists_what_the_server_lets_them_read` | `ivan` видит «Общие» и «Кадры», `maria` — только «Общие»; png не в листинге; путь до третьего уровня вложенности |
| `every_format_reaches_the_extractor` | docx, pdf, md, txt скачиваются и разбираются общим ingest |
| `wrong_password_is_auth_failed` | неверный пароль → `auth_failed` |
| `folder_deeper_than_the_limit_stops_the_listing` | 32 уровня папок обходятся, 33-й — `tree_too_deep`, а не обрезанный листинг |
| `sync_mirrors_access_and_follows_changes` | полная синхронизация с двумя сотрудниками: общий файл — копия на каждого (сквозного id у NAS нет), каждая — только своему; закрытая папка — только `ivan`; png — «пропущенный формат»; правка файла → `updated`, удаление → `removed` |
| `wrong_password_expires_only_that_grant` | неверный пароль у одного сотрудника гасит только его подключение |
| `too_deep_tree_fails_the_run_without_deleting` | запуск с деревом глубже 32 уровней — FAILED, материалы не удалены |
| `connector_check_records_without_the_password` | `cli connector-check --fetch 2 --record`: в записанных ответах нет пароля |

## Итоги 09.10.2026

- 8 из 8 тестов.
- **Найдено и исправлено:** Apache на папку с `Require user` другого
  пользователя отвечает **401, а не 403**, хотя пароль верный. Адаптер
  считал это неверным паролем: подключение `maria` гасло на первой же
  синхронизации, и следующей её материалы удалялись. Теперь на 401 во
  вложенной папке адаптер переспрашивает корень: пускает — папка
  пропускается как закрытая; не пускает — `auth_failed` (пароль отозван
  посреди обхода).
- Ссылки в ответе — со строчными %-кодами (`%d0%9e…`), etag вида
  `651-65d6…`, `oc:fileid` нет — id свой у каждого сотрудника.
- 20 001 файл в одной папке (созданы прямо в томе: `docker compose exec
  webdav sh -c 'mkdir /dav/files/Много && cd /dav/files/Много && seq 1
  20001 | sed "s/$/.txt/" | xargs touch'`) — `tree_too_large` за 5 секунд,
  ответ PROPFIND ~10 МБ укладывается в лимит 16 МБ.
- Образ `httpd:*-alpine` не годится: в нём нет драйвера DBM для
  `DavLockDB`, и любая запись отвечает 500. Взят Debian-вариант.

## Что стенд не проверяет

- Настоящие NAS (Synology DSM, QNAP): там свои корзины и служебные папки
  (`#recycle`, `@eaDir`) — адаптер их пропускает по имени, но живьём не
  проверено.
