# Живой Outline в Docker

Стенд для проверки вида `outline` (режим organization) на официальном
образе `outlinewiki/outline`: Postgres, Redis, dex (OIDC — без внешнего
входа Outline не пускает), TLS-прокси nginx на 127.0.0.1 и выдуманная
компания — администратор `admin` (владелец API-ключа), `ivan`, `maria`,
`petr`, группа `hr` (`maria`, `petr`), коллекция «Общая» для всей
рабочей области, закрытые «Кадры» (группа `hr`) и «Проекты» (`ivan`),
вложенный документ «Бюджет» с участником документа `maria`. Тесты —
`tests/live/test_outline_live.py`; без переменных окружения они
пропускаются, в CI не идут.

Проверено 09.10.2026 на **Outline 1.10.1** (dex 2.44.0, Postgres 16.15,
Redis 7.4.11, nginx 1.31.6) — итоги внизу.

## Что нужно

- Docker, ~1 ГБ диска на образы, ~1 ГБ памяти.
- Node и Playwright из `frontend/` (`npm ci` там) — вход через dex в
  браузере; в песочнице Claude браузер — в `/opt/pw-browsers`:
  `export CHROMIUM_PATH="$(ls -d /opt/pw-browsers/chromium-*/chrome-linux/chrome | tail -1)"`.
- При 429 от Docker Hub — `mirror.gcr.io/outlinewiki/outline:latest`
  (и `mirror.gcr.io/library/redis:7-alpine`) и `docker tag` в имена из
  compose.yaml; dex — с `ghcr.io/dexidp/dex`.

Ключи `SECRET_KEY`/`UTILS_SECRET`, секрет клиента dex и пароль
пользователей (`Kronto-Test-1`, в `dex.yaml` — его bcrypt) — выдуманные,
только для стенда на 127.0.0.1.

## Как поднять (≈5 минут)

1. Сертификат стенда — как в `../nextcloud/README.md`, шаг 1
   (`DAV_TLS_DIR`, `DAV_STAND_CA`).

2. Запустить (Outline — порт 8447, dex — 5556) и дождаться `OK`:

   ```bash
   docker compose -f tests/live/outline/compose.yaml up -d
   curl -s --cacert ~/dav-tls/cert.pem https://127.0.0.1:8447/_health
   ```

3. Вход пользователей (Outline заводит учётку при первом входе; первый
   вошедший — администратор). Администратор печатает API-ключ, `ivan` —
   ключ участника (для проверки, что такой ключ не принимается):

   ```bash
   cd frontend
   node ../tests/live/outline/login.mjs https://127.0.0.1:8447/ admin@example.com Kronto-Test-1 --api-key > ~/outline-admin.key
   node ../tests/live/outline/login.mjs https://127.0.0.1:8447/ maria@example.com Kronto-Test-1
   node ../tests/live/outline/login.mjs https://127.0.0.1:8447/ petr@example.com Kronto-Test-1
   node ../tests/live/outline/login.mjs https://127.0.0.1:8447/ ivan@example.com Kronto-Test-1 --api-key > ~/outline-ivan.key
   cd ..
   ```

4. Засеять компанию (только на чистом стенде):

   ```bash
   OUTLINE_URL=https://127.0.0.1:8447/ OUTLINE_TOKEN="$(cat ~/outline-admin.key)" \
     uv run python -m tests.live.outline.seed
   ```

5. Тесты:

   ```bash
   OUTLINE_URL=https://127.0.0.1:8447/ OUTLINE_TOKEN="$(cat ~/outline-admin.key)" \
   OUTLINE_MEMBER_TOKEN="$(cat ~/outline-ivan.key)" \
     uv run pytest tests/live/test_outline_live.py -q --no-cov
   ```

6. `cli connector-check` — как в `../webdav/README.md`, шаг 5, с
   `--stand https://127.0.0.1:8447 -- --kind outline --config
   base_url=https://127.0.0.1:8447/ --credential token=…`.

7. Убрать стенд: `docker compose -f tests/live/outline/compose.yaml down -v`.

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `listing_mirrors_outline_permissions` | Открытая коллекция — вся компания; закрытая — участники коллекции, участники групп коллекции и участники самого документа (почты из `users.list`); путь «Коллекция/Родитель»; ссылка `/doc/…` |
| `documents_come_as_markdown` | Текст — Markdown из `documents.list` с заголовком |
| `wrong_key_is_an_auth_error` | Неверный ключ → `AdapterAuthError("unauthorized")`, как у остальных видов с токеном |
| `member_key_is_refused` | Ключ участника (не администратора) → `admin_required` |
| `sync_mirrors_access_and_follows_changes` | Полная синхронизация: каждый сотрудник kronto видит ровно то, что Outline; правка текста → `updated`, удалённый документ → `removed`, выход из группы снимает доступ без переиндексации |
| `connector_check_records_without_the_key` | `cli connector-check --fetch 2 --record`: ключа в записи нет |

## Итоги 09.10.2026

- 6 из 6 тестов (≈5 секунд).
- **Найдено и исправлено:** группы коллекции Outline 1.10 отдаёт в
  `data.groupMemberships`, а адаптер читал только
  `collectionGroupMemberships` — участники группы с доступом к закрытой
  коллекции не получали доступа в kronto. Теперь читаются оба ключа; записи стенда — фикстуры
  контракта (`tests/connectors/fixtures/outline/live`).
- Новая рабочая область создаёт открытую коллекцию Welcome с
  документами Outline — она индексируется как «вся компания».
- Создать API-ключ из сессии браузера можно только с заголовком
  `x-csrf-token` (значение cookie `__Host-csrfToken`) — `login.mjs`
  делает это из страницы, как интерфейс.

## Что стенд не проверяет

- Облако `app.getoutline.com` (тот же код, но живьём не открывали).
- Гостей и общий доступ по ссылке (публикация документа наружу).
- Закрытую коллекцию, где владельца ключа нет: такие документы ключ не
  видит и не индексирует (описано в адаптере), живьём не воспроизводили.
- Yonote — у неё свой диалект, отдельной живой проверки нет.
