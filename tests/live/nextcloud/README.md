# Живой Nextcloud в Docker

Стенд для проверки вида `nextcloud` (пароль приложения) и
`nextcloud_oauth` на официальном образе Nextcloud: Postgres, TLS-прокси
nginx на 127.0.0.1 и выдуманная компания — три пользователя (`ivan`,
`maria`, `petr`), группа `hr` (`maria`, `petr`), у `ivan` папка
«Проекты», расшаренная `maria`, папка «Кадры», расшаренная группе, и
файл «Общий.txt», расшаренный и `maria`, и группе сразу. Тесты —
`tests/live/test_nextcloud_live.py` и `test_nextcloud_oauth_live.py`; без переменных окружения они пропускаются, в CI не идут.

Проверено 09.10.2026 на **Nextcloud 34.0.4** (`nextcloud:34.0.4-apache`,
Postgres 16.15, nginx 1.31.6) — итоги внизу.

## Что нужно

- Docker, ~2,5 ГБ диска на образы, ~1 ГБ памяти.
- Postgres и Redis для тестов синхронизации — как у остального набора.
- Если Docker Hub отвечает 429 (лимит анонимных скачиваний), образы
  берутся с зеркала: `docker pull mirror.gcr.io/library/nextcloud:34.0.4-apache`
  и `docker tag` в имя из compose.yaml (так же для postgres и nginx).

Почему HTTPS и прокси: адаптер ходит только по https, а образ Nextcloud
слушает http — перед ним nginx с сертификатом стенда
(`../tls-proxy.conf.template`, подробнее — `../dav_stand.py`).

## Как поднять (≈3 минуты)

Все команды — из корня репозитория.

1. Сертификат стенда (вне репозитория; один на все стенды WebDAV):

   ```bash
   uv run python -m tests.live.dav_stand tls ~/dav-tls
   export DAV_TLS_DIR=~/dav-tls DAV_STAND_CA=~/dav-tls/cert.pem
   ```

2. Запустить (порт 8444, другой — `NEXTCLOUD_PORT`) и дождаться
   установки — `"installed":true` (1–2 минуты):

   ```bash
   docker compose -f tests/live/nextcloud/compose.yaml up -d
   curl -s --cacert ~/dav-tls/cert.pem https://127.0.0.1:8444/status.php
   ```

3. Засеять компанию (только на чистом стенде: шары создаются заново):

   ```bash
   NEXTCLOUD_URL=https://127.0.0.1:8444/ uv run python -m tests.live.nextcloud.seed
   ```

4. Тесты. Пароли приложений тесты получают сами из паролей входа
   (`/ocs/v2.php/core/getapppassword` — то же, что «Создать новый пароль
   приложения» в настройках):

   ```bash
   NEXTCLOUD_URL=https://127.0.0.1:8444/ \
     uv run pytest tests/live/test_nextcloud_live.py -q --no-cov
   ```

   Тесты с неверным паролем идут последними: Nextcloud запоминает
   неудачные входы с адреса и замедляет следующие запросы (до десятков
   секунд). Сбросить: `docker compose -f tests/live/nextcloud/compose.yaml
   exec -u www-data nextcloud php occ security:bruteforce:reset <адрес>`
   (адрес — в журнале Nextcloud, обычно шлюз сети Docker).

5. `cli connector-check` против стенда — как в `../webdav/README.md`,
   шаг 5, с `--kind nextcloud --config server=https://127.0.0.1:8444/`
   и паролем приложения сотрудника.

6. OAuth2 (`nextcloud_oauth`): клиент — через `occ` (секрет печатается
   один раз; сохраните вне репозитория), вход сотрудника — в браузере
   Playwright из `frontend/` (`npm ci` там; в песочнице Claude браузер —
   в `/opt/pw-browsers`, тест находит его сам):

   ```bash
   docker compose -f tests/live/nextcloud/compose.yaml exec -u www-data nextcloud \
     php occ oauth2:add-client --output=json kronto-stand \
     https://kronto.example.ru/api/v1/connectors/oauth/callback > ~/nc-oauth.json
   NEXTCLOUD_URL=https://127.0.0.1:8444/ \
   NEXTCLOUD_OAUTH_CLIENT_ID="$(jq -r .clientId ~/nc-oauth.json)" \
   NEXTCLOUD_OAUTH_CLIENT_SECRET="$(jq -r .clientSecret ~/nc-oauth.json)" \
     uv run pytest tests/live/test_nextcloud_oauth_live.py -q --no-cov
   ```

   Адрес возврата браузер не открывает: `oauth.mjs` перехватывает переход
   и берёт код из адреса (другой адрес — `NEXTCLOUD_OAUTH_CALLBACK`).

7. Убрать стенд вместе с данными:

   ```bash
   docker compose -f tests/live/nextcloud/compose.yaml down -v
   ```

Пароли пользователей — `Kronto-Test-1`, администратора — `Kronto-Admin-1`
(выдуманные, стенд только на 127.0.0.1).

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `shared_file_is_one_document_for_everyone_who_sees_it` | Общий файл и файлы общих папок — один `external_id` (`nextcloud:id:<fileid>`) у всех, кто их видит; файл, расшаренный и пользователю, и его группе, у него один; одинаковый путь у двух сотрудников — разные документы; png не в листинге |
| `every_format_reaches_the_extractor` | docx, pdf, md, txt скачиваются и разбираются; ссылка — `index.php/f/<fileid>` |
| `folder_deeper_than_the_limit_stops_the_listing` | 32 уровня папок обходятся, 33-й — `tree_too_deep` |
| `sync_gives_each_employee_only_what_nextcloud_shows_them` | Полная синхронизация трёх сотрудников: один материал на общий файл, доступ — ровно тем, кто его видит; правка → `updated`, удаление файла в общей папке → `removed`, снятая шара → доступ только владельцу |
| `connector_check_records_without_the_password` | `cli connector-check --fetch 2 --record`: в записи есть `oc:fileid`, нет ни пароля приложения, ни пароля входа |
| `wrong_password_is_auth_failed` | неверный пароль приложения → `auth_failed` |
| `wrong_password_expires_only_that_grant` | гаснет только подключение с неверным паролем, у остальных всё как было |
| `login_exchange_and_listing_by_token` (OAuth2) | вход в браузере → код → токены и `user_id`; листинг по Bearer; повторный обмен кода — `invalid_grant` |
| `refresh_rotates_the_pair_and_the_old_refresh_dies` (OAuth2) | продление выдаёт новую пару, старый refresh больше не действует; просроченный токен адаптер продлевает сам |
| `wrong_client_secret_is_a_connector_error` (OAuth2) | неверный секрет клиента → `invalid_client` (ошибка подключения, а не сотрудника) |
| `sync_by_tokens_keeps_one_document_per_shared_file` (OAuth2) | синхронизация по токенам двух сотрудников: один материал на общий файл; продлённые токены сохранены в грантах |

## Итоги 09.10.2026

- Пароль приложения — 7 из 7 тестов (≈1,5 минуты), OAuth2 — 4 из 4
  (≈1 минута, вход в браузере).
- **Найдено и исправлено (безопасность):** клиент HTTP для запросов
  наружу один на процесс, и httpx хранил cookie из ответов. Nextcloud
  ставит cookie сессии на первый вход по паролю приложения и по ней
  пускает следующий запрос **даже с неверным паролем другой учётки**:
  синхронизация сотрудника с отозванным паролем листала файлы первого
  сотрудника. Теперь `OutboundClient` не отправляет cookie из банки, а
  у клиента процесса банка ничего не хранит (`core/outbound.py`).
- Общие папки и файлы лежат в корне дерева получателя под своим именем
  (`share_folder` по умолчанию), `oc:fileid` у всех один — обещание
  каталога «один документ на всех» выполняется.
- Новому пользователю Nextcloud кладёт файлы-образцы (Readme.md,
  «Welcome to Nextcloud Hub.docx» и т. п.): они индексируются как личные
  файлы каждого;
  `.odt`, `.whiteboard`, `.jpg` и т. п. — «пропущенный формат».
- OAuth2 ведёт себя как в описании модуля `connectors/webdav/oauth.py`:
  токен на час, `user_id` в ответе, новая пара при каждом продлении,
  повторный обмен кода и старый refresh — `invalid_request`.
- `current-user-principal` на `/remote.php/dav/` отдаёт
  `/remote.php/dav/principals/users/<uid>/` — uid находится без логина.

## Что стенд не проверяет

- LDAP и вход по почте (uid ≠ логин) — пользователи во внутреннем
  каталоге, uid = логин.
- Групповые папки (Team folders) и внешние хранилища — только обычные
  шары.
- Адрес с путём (`https://host/nextcloud/`).
