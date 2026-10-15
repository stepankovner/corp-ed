# Живой Wiki.js в Docker

Стенд для проверки вида `wikijs` (режим organization) на официальном
образе `ghcr.io/requarks/wiki:2`: Postgres, TLS-прокси nginx на
127.0.0.1 и выдуманная компания — администратор `admin`, сотрудники
`ivan`, `maria`, `petr` и отключённая `olga`; группы «Сотрудники» (все
четверо: читают всё, кроме `hr`, `reports`, тега `confidential` и
черновиков на локали ru), «Кадры» (`maria`: `hr/` — длиннее запрета
`hr`, и отчёты по REGEX `^reports/\d{4}$`), «Подрядчики» (`petr`: запрет
`projects`); гостям открыт только раздел `public`. Страницы — на локалях
en и ru, одна неопубликованная. Тесты — `tests/live/test_wikijs_live.py`;
без переменных окружения они пропускаются, в CI не идут.

Проверено 15.10.2026 на **Wiki.js 2.5.316** (`ghcr.io/requarks/wiki:2`,
Postgres 16.15, nginx 1.31.6) — итоги внизу. Код прав читался по тегу
v2.5.315 (`server/core/auth.js` в образе 2.5.316 — тот же файл).

## Что нужно

- Docker, ~0,8 ГБ диска на образы, ~0,5 ГБ памяти.
- Postgres и Redis для тестов синхронизации — как у остального набора.
- При 429 от Docker Hub — `mirror.gcr.io/library/postgres:16.15-alpine`
  и `mirror.gcr.io/library/nginx:1.31.6-alpine` и `docker tag` в имена
  из compose.yaml. Образ Wiki.js — с ghcr.io.

Пароль пользователей (`Kronto-Test-1`) и пароль базы — выдуманные,
только для стенда на 127.0.0.1. Ключи API печатает seed.py — храните их
вне репозитория.

## Как поднять (≈3 минуты)

Все команды — из корня репозитория.

1. Сертификат стенда — как в `../nextcloud/README.md`, шаг 1. Обе
   переменные нужны и дальше: seed.py вызывает `docker compose exec`, а
   compose без `DAV_TLS_DIR` файл не разбирает.

   ```bash
   uv run python -m tests.live.dav_stand tls ~/dav-tls
   export DAV_TLS_DIR=~/dav-tls DAV_STAND_CA=~/dav-tls/cert.pem
   ```

2. Запустить (порт 8470, другой — `WIKIJS_PORT`):

   ```bash
   docker compose -f tests/live/wikijs/compose.yaml up -d
   curl -s --cacert ~/dav-tls/cert.pem https://127.0.0.1:8470/healthz
   ```

3. Установить и засеять (только на чистом стенде). seed.py проходит
   мастер установки (`/finalize`), добавляет локаль ru строкой в таблицу
   `locales` (скачать её стенд не может: нет выхода к graph.requarks.io),
   заводит пользователей, группы с правилами и страницы, включает API и
   печатает два ключа — с полным доступом и группы «Сотрудники».
   Уже установленный стенд — `--no-install`.

   ```bash
   WIKIJS_URL=https://127.0.0.1:8470/ \
     uv run python -m tests.live.wikijs.seed > ~/wikijs-keys.env
   ```

4. Тесты. Вход в Wiki.js ограничен пятью попытками в минуту (тест сверки
   прав входит тремя сотрудниками) — между запусками подождите минуту.

   ```bash
   set -a; . ~/wikijs-keys.env; set +a
   WIKIJS_URL=https://127.0.0.1:8470/ \
     uv run pytest tests/live/test_wikijs_live.py -q --no-cov
   ```

5. `cli connector-check` — как в `../webdav/README.md`, шаг 5, с
   `--stand https://127.0.0.1:8470 -- --kind wikijs --config
   base_url=https://127.0.0.1:8470/ --credential token=…` (пока вид не
   добавлен в `default_registry`, это делает тест
   `connector_check_records_without_the_key`).

6. Убрать стенд: `docker compose -f tests/live/wikijs/compose.yaml down -v`.

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `listing_mirrors_seeded_rules` | Страница гостей — вся компания; остальные — почты тех, кому их открывают правила групп (запрет при равной длине, более длинный путь, TAG, REGEX, локаль, группа без прав с одним запретом); отключённой нет; неопубликованной страницы нет; путь «локаль/папка» |
| `listing_matches_wikijs_own_answer` | Права адаптера совпадают с ответом самого Wiki.js: гость и сотрудники открывают каждую страницу (`/{локаль}/{путь}`, 200 или 403) |
| `pages_come_as_rendered_html` | Содержимое — готовый HTML (`render`) без якорей «¶», заголовок один раз |
| `wrong_key_is_an_auth_error` | Испорченный ключ Wiki.js обслуживает как гостя — `AdapterAuthError("unauthorized")` |
| `revoked_key_is_an_auth_error` | Отозванный ключ (HTTP 500 «API Key is invalid or was revoked») — тоже `unauthorized`, без повторов |
| `group_key_is_refused` | Ключ группы (не «Full Access») → `admin_required` |
| `sync_mirrors_access_and_follows_changes` | Полная синхронизация: каждый сотрудник kronto видит ровно то, что в Wiki.js; правка текста → `updated`, удалённая страница → `removed`, выход из группы снимает доступ без переиндексации |
| `connector_check_records_without_the_key` | `cli connector-check --fetch 2 --record`: ключа и стека сервера в записи нет |

## Итоги 15.10.2026

- 8 из 8 тестов (≈15 секунд); права всех 9 страниц совпали с ответом
  Wiki.js для гостя, `ivan`, `maria`, `petr` (администратор видит всё).
- Ошибки полей GraphQL приходят с HTTP 200 рядом с `data` (`Forbidden`,
  код 6003 «страницы нет», 1019 «нужен вход»); отозванный ключ и
  выключенный API — HTTP 500 с текстом в `errors`, испорченный ключ —
  ответ гостю. Адаптер различает все три.
- `publishStartDate`/`publishEndDate` без значения — пустая строка.
- `pages.update` переписывает страницу целиком: без `isPublished` она
  становится неопубликованной, без `tags` запрос падает, успев сохранить
  часть полей (касается только seed.py и тестов — адаптер ничего не
  пишет).
- В новой установке у группы Guests правило START "" — вся вики открыта
  без входа и станет в kronto «всей компанией», пока его не сузят (seed.py
  сужает до `public`).

## Что стенд не проверяет

- `namespacing` локалей (у стенда выключен; адрес `/{локаль}/{путь}`
  работает в обоих режимах по `parsePath`).
- Вход через внешних провайдеров (LDAP, OIDC): почты берутся из
  `users.list`, как для локальных учёток.
- REGEX вне переносимого подмножества и окно публикации — только в
  контрактных тестах (`tests/connectors/test_wikijs_adapter.py`).
- Очень большую вику: `pages.list` без постраничности, ответ больше
  20 МБ остановит обход.
