# Живой Confluence в Docker (Р-12 «б»)

Стенд для проверки адаптера `connectors/confluence/` на настоящем
Confluence Server/DC: выдуманная компания (5 пользователей, 2 группы,
2 пространства, 7 страниц с ограничениями чтения и 2 вложения) и тесты
`tests/live/test_confluence_dc_live.py`. Без переменных окружения тесты
пропускаются — в CI они не запускаются.

Проверено 01.10.2026 на **7.19.30, 8.5.31 и 10.2.17** — итоги внизу.

## Что нужно

- Docker и ~3 ГБ памяти на одну версию (Confluence + Postgres), ~3 ГБ
  диска на образ.
- **Тестовая лицензия Atlassian.** Страница «Timebomb licenses for
  testing server apps» на developer.atlassian.com (раздел Marketplace),
  ключ **«Confluence Data Center license, expires in 3 hours»**: 10
  пользователей, Data Center, действует 3 часа с запуска сервера. Ключ
  публичный, но в репозиторий его не кладём — сохраните в файл вне
  репозитория. Через 3 часа стенд перестаёт пускать на запись:
  пересоздайте его (шаг 6 и заново).
- Node и Playwright из `frontend/` (`npm ci` там) — мастер первого
  запуска Confluence проходится в браузере. В песочнице Claude браузер
  указывается через `CHROMIUM_PATH=/opt/pw-browsers/chromium-*/chrome-linux/chrome`.

## Как поднять (≈10 минут)

Все команды — из корня репозитория, кроме тех, где сказано `cd frontend`.

1. Запустить Confluence (по умолчанию 10.2.17 на порту 8090):

   ```bash
   export CONFLUENCE_LICENSE="$(cat ~/confluence_dc_timebomb.key)"
   docker compose -f tests/live/confluence_dc/compose.yaml up -d
   # ждать, пока ответит {"state":"FIRST_RUN"} (2–3 минуты):
   curl -s http://127.0.0.1:8090/status
   ```

   Другая версия — переменные `CONFLUENCE_VERSION`, `CONFLUENCE_PORT` и
   своё имя проекта, тогда версии живут рядом:

   ```bash
   CONFLUENCE_VERSION=7.19.30 CONFLUENCE_PORT=8091 \
     docker compose -p corp-ed-confluence-719 -f tests/live/confluence_dc/compose.yaml up -d
   ```

2. Мастер первого запуска. Печатает токен администратора последней
   строкой (`ADMIN_TOKEN=…`, живёт сутки), заодно выключает WebSudo
   (нужно 7.x — см. ниже):

   ```bash
   cd frontend
   CONFLUENCE_URL=http://127.0.0.1:8090 node ../tests/live/confluence_dc/wizard.mjs > ~/wizard.out
   cd ..
   ```

3. Засеять компанию (повторный запуск упадёт на занятых именах — только
   на чистом стенде):

   ```bash
   CONFLUENCE_URL=http://127.0.0.1:8090 \
   CONFLUENCE_ADMIN_TOKEN="$(sed -n 's/^ADMIN_TOKEN=//p' ~/wizard.out)" \
     uv run python -m tests.live.confluence_dc.seed
   ```

4. Токен служебной учётки `svc-kronto` — с ним работает адаптер:

   ```bash
   cd frontend
   CONFLUENCE_URL=http://127.0.0.1:8090 \
     node ../tests/live/confluence_dc/token.mjs svc-kronto Kronto-Test-1 > ~/svc.token
   cd ..
   ```

5. Тесты (нужны Postgres и Redis для тестов, как у остального набора):

   ```bash
   CONFLUENCE_DC_URL=http://127.0.0.1:8090 \
   CONFLUENCE_DC_TOKEN="$(cat ~/svc.token)" \
   CONFLUENCE_DC_ADMIN_TOKEN="$(sed -n 's/^ADMIN_TOKEN=//p' ~/wizard.out)" \
     uv run pytest tests/live/test_confluence_dc_live.py -q --no-cov
   ```

   Без `CONFLUENCE_DC_ADMIN_TOKEN` пропускается только тест отзыва прав:
   он меняет права на стенде и возвращает их в `finally`.

6. Убрать стенд вместе с данными:

   ```bash
   docker compose -f tests/live/confluence_dc/compose.yaml down -v
   ```

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `listing_mirrors_confluence_read_restrictions` | Читатели каждой страницы = правила Confluence: наследование от родителя, пересечение по цепочке, пользователь + группа — объединение; страницу, которой служебная учётка не видит, адаптер не возвращает; вложения наследуют читателей; путь «Кадры/Зарплаты»; ссылки открываются |
| `login_and_password_where_basic_auth_is_on` | Вход логином и паролем там, где Basic в REST включён; на 10.x — понятный отказ `basic_auth_disabled` (тест пропускается) |
| `page_and_attachment_reach_the_extractor` | Страница → HTML, вложение .docx → текст через общий разбор |
| `sync_gives_each_employee_only_what_confluence_lets_them_read` | Полная синхронизация в базу: каждый сотрудник kronto (та же почта) видит ровно то, что ему разрешает Confluence |
| `access_revoked_in_confluence_leaves_kronto_on_next_sync` | Отзыв прав без новой версии страницы (сотрудник вышел из группы, на открытую страницу поставили ограничение, страницу удалили) доходит до kronto следующим запуском; переиндексации нет |

Ожидания — в `seed.py` (`EXPECTED_READERS`): кто по правилам Confluence
читает каждую страницу. `admin` есть в каждом ограничении: Confluence не
даёт ограничить страницу так, чтобы её автор потерял доступ.

## Итоги 01.10.2026

| | 7.19.30 | 8.5.31 | 10.2.17 |
|---|---|---|---|
| Тесты | 5 из 5 | 5 из 5 | 4 + 1 пропуск (Basic выключен) |
| Состав группы (`group/{name}/member`) обычной учётке | 401 → обратный ход | 401 → обратный ход | отдаёт |
| Вход логином и паролем (Basic) | да | да | нет, только токен |
| Почта пользователя в REST | нет (даже администратору) | нет | есть |
| Ссылка на страницу из ответа | `/pages/viewpage.action?pageId=…` | то же | `/spaces/HR/pages/…` |
| Управление пользователями для `seed.py` | JSON-RPC (нужен выключенный WebSudo) | REST | REST |
| Запись ограничений для `seed.py` | `/rest/experimental/…` | `/rest/api/…` | `/rest/api/…` |

«Обратный ход» — адаптер собирает состав групп сам: все пользователи
(CQL `type=user`) → группы каждого (`user/memberof`), один раз на запуск,
до 2 000 пользователей (`DIRECTORY_LIMIT`); больше — группы не
раскрываются, права по ним никому не выдаются. Подробно — DECISIONS
«Confluence: живая проверка 01.10», RISKS №37.

## Что стенд не проверяет

- Confluence 9.x и адрес с путём (`https://host/confluence/`).
- Каталог LDAP/AD и SSO — пользователи здесь во внутреннем каталоге.
- Права пространств: REST их не отдаёт (RISKS №37) — открытая страница
  видна всей компании, пространства выбирает админ подключения.
- Обратный ход на тысячах пользователей — только на шести.
