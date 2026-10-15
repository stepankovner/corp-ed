# Живая Jira Software DC в Docker

Стенд для проверки вида `jira_dc` (`connectors/jira_dc/`) на настоящей
Jira Software Data Center: выдуманная компания (7 пользователей с
почтами, из них одна деактивирована, 2 группы, роль проекта, 3 проекта с
разными схемами прав, схема уровней безопасности с двумя уровнями,
6 задач с комментариями, комментарий с ограничением видимости и
вложение .docx) и тесты `tests/live/test_jira_dc_live.py`. Без
переменных окружения тесты пропускаются — в CI они не запускаются.

Проверено 15.10.2026 на **Jira Software 10.3.26** (Postgres 15.19) —
итоги внизу.

## Что нужно

- Docker, ~2,2 ГБ диска на образ Jira и **4 ГБ памяти** на контейнер:
  куча Jira — 2 ГБ (`JVM_MAXIMUM_MEMORY`), но с метаданными, кешами
  плагинов и Tomcat контейнер с пределом 3 ГБ убивается по памяти
  (OOM) в конце мастера — поэтому `mem_limit: 4g`. Стенд держите
  поднятым не дольше проверки.
- При 429 от Docker Hub — `mirror.gcr.io/atlassian/jira-software:10.3.26`
  и `mirror.gcr.io/library/postgres:15-alpine`, затем `docker tag` в
  имена из compose.yaml.
- **Тестовая лицензия Atlassian** — та же страница, что для Confluence
  (`../confluence_dc/README.md`): «Timebomb licenses for testing server
  apps» на developer.atlassian.com, ключ **«10 user Jira Software Data
  Center license, expires in 3 hours»**. В отличие от образа Confluence,
  переменной для лицензии у образа Jira нет — её вводит мастер
  (`JIRA_LICENSE`). Ключ в репозиторий не кладём. Через 3 часа с
  запуска сервера стенд перестаёт пускать на запись: пересоздайте его.
- Node и Playwright из `frontend/` (`npm ci` там) — мастер первого
  запуска и схема уровней безопасности проходятся в браузере. В
  песочнице Claude браузер уже стоит:
  `export CHROMIUM_PATH="$(ls -d /opt/pw-browsers/chromium-*/chrome-linux/chrome | head -1)"`.

Пароли (`admin-Test-1`, `Kronto-Test-1`, базы — `jira-Test-1`) —
выдуманные, только для стенда на 127.0.0.1.

## Как поднять (≈15 минут)

Все команды — из корня репозитория, кроме тех, где сказано `cd frontend`.

1. Запустить Jira (порт 8510) и дождаться `{"state":"FIRST_RUN"}`
   (≈2 минуты):

   ```bash
   docker compose -f tests/live/jira_dc/compose.yaml up -d
   curl -s http://127.0.0.1:8510/status
   ```

2. Мастер первого запуска: название, лицензия, администратор `admin`,
   почта «позже». Печатает токен администратора последней строкой
   (`ADMIN_TOKEN=…`, живёт сутки). Последний шаг мастера занимает
   несколько минут:

   ```bash
   cd frontend
   JIRA_LICENSE="$(cat ~/jira_dc_timebomb.key)" \
     node ../tests/live/jira_dc/wizard.mjs > ~/jira-wizard.out
   cd ..
   export JIRA_URL=http://127.0.0.1:8510
   export JIRA_ADMIN_TOKEN="$(sed -n 's/^ADMIN_TOKEN=//p' ~/jira-wizard.out)"
   ```

3. Пользователи, группы, роль Developers, схемы прав и проекты (только
   на чистом стенде):

   ```bash
   uv run python -m tests.live.jira_dc.seed rest
   ```

4. Схема уровней безопасности «Kronto Секретность» с уровнями
   «Руководство» и «Без службы» — в браузере: REST Jira DC схемы и
   уровни только читает. Скрипт же привязывает её к проекту ENG
   (`PUT /rest/api/2/project/ENG`); повторный запуск ничего не дублирует:

   ```bash
   cd frontend && node ../tests/live/jira_dc/security.mjs && cd ..
   ```

5. Задачи, комментарии и вложение:

   ```bash
   uv run python -m tests.live.jira_dc.seed issues
   ```

6. Токены служебной учётки `svc-kronto` (администратор Jira — с ней
   работает адаптер) и обычного пользователя `bob`. В 10.3 Basic в REST
   включён, токен выдаётся без браузера; где выключен — как в
   `../confluence_dc/token.mjs`:

   ```bash
   uv run python -m tests.live.jira_dc.seed token > ~/jira-svc.token
   uv run python -m tests.live.jira_dc.seed token bob > ~/jira-bob.token
   ```

7. Тесты (нужны Postgres и Redis для тестов, как у остального набора):

   ```bash
   JIRA_DC_URL=http://127.0.0.1:8510 \
   JIRA_DC_TOKEN="$(cat ~/jira-svc.token)" \
   JIRA_DC_MEMBER_TOKEN="$(cat ~/jira-bob.token)" \
   JIRA_DC_ADMIN_TOKEN="$JIRA_ADMIN_TOKEN" \
     uv run pytest tests/live/test_jira_dc_live.py -q --no-cov
   ```

   Без `JIRA_DC_MEMBER_TOKEN` пропускаются тесты обычного пользователя
   и администратора проекта, без `JIRA_DC_ADMIN_TOKEN` — тесты, которые
   меняют стенд (и возвращают его в `finally`).

8. Убрать стенд вместе с данными и образ:

   ```bash
   docker compose -f tests/live/jira_dc/compose.yaml down -v
   docker image rm atlassian/jira-software:10.3.26
   ```

## Тестовая компания

| Проект | Browse Projects в схеме прав | Задачи |
|---|---|---|
| HR «Кадры» | группа `hr`, «Автор задачи», `admin` | «Отпуск» (2 комментария, 1 — только для роли Administrators, вложение .docx), «Справка для Дэйва» (автор — `dave`) |
| ENG «Разработка» | роль Developers (группа `engineering` и `dave`), `admin` | «Архитектура»; «Инцидент с данными» — уровень «Руководство» (`carol`, `svc-kronto`, `admin`); «Оценка руководителя» — уровень «Без службы» (`carol`, `admin`), служебной учётке не видна |
| OPEN «Общее» | «Любой» (анонимный доступ), `admin` | «Регламент» |

Группы: `hr` — alice, carol, erin, svc-kronto; `engineering` — bob,
carol, svc-kronto. `erin` деактивирована. `svc-kronto` ещё и в
`jira-administrators`. Ожидания — в `seed.py` (`EXPECTED_READERS`).

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `listing_mirrors_jira_permissions` | Читатели каждой задачи = ответ Jira: группа, «Автор задачи» (dave видит свою задачу), роль проекта, уровень безопасности сужает роль, «Любой» — все активные (деактивированная erin — нет); задачу вне видимости служебной учётки адаптер не возвращает; вложение наследует читателей задачи; путь «Кадры», ссылки `/browse/…` |
| `without_project_list_all_visible_projects_are_walked` | Без списка проектов обходятся все видимые, фильтр JQL (`updated >= -30d`) встаёт в запрос |
| `issue_and_attachment_reach_the_extractor` | Задача → HTML с описанием и комментариями, комментарий с ограничением видимости в материал не попадает (служебная учётка его видит); .docx → текст через общий разбор |
| `token_of_plain_user_is_refused` | Токен обычного пользователя: Jira не называет читателей ни в одном проекте (403) → `admin_required`, а не «видно всем» |
| `project_admin_token_covers_only_its_projects` | Администратор одного проекта: индексируется только его проект, задачи проекта, где он лишь читатель, — нет |
| `sync_gives_each_employee_only_what_jira_lets_them_read` | Полная синхронизация в базу: каждый сотрудник kronto (та же почта) видит ровно то, что ему открывает Jira |
| `access_revoked_in_jira_leaves_kronto_on_next_sync` | Сотрудник вышел из группы — доступ снимается следующим запуском без новой версии задачи и без переиндексации; удалённая задача удаляется |

## Итоги 15.10.2026 (10.3.26)

7 из 7 тестов. Что выяснилось на живой Jira и вошло в адаптер:

| | 10.3.26 |
|---|---|
| Ключ права в `/user/permission/search` | только старый `BROWSE`; `BROWSE_PROJECTS` — 400 «Invalid permission name» |
| Все пользователи в этом поиске | `username=` (пусто); `username=.` отдаёт не всех |
| Кому доступен этот поиск | администратору Jira или проекта; остальным — 403 |
| С `issueKey` учитываются | уровень безопасности, «Автор задачи», неактивные исключены |
| С `projectKey` при «Авторе задачи» в схеме | надмножество (bob «видит» HR) — поэтому спрашиваем по задаче |
| Состав уровней безопасности в REST | нет (`/issuesecurityschemes/{id}` — только уровни, `/members` — 404) |
| Создание схемы уровней в REST | нет — `security.mjs` |
| Схема прав проекта (`/project/{key}/permissionscheme`) | администратору Jira или проекта; обычному — 403 |
| Комментарий с видимостью «роль» | администратор видит его в задаче — адаптер отбрасывает такие комментарии |
| Видимость комментария «группа» | выключена по умолчанию (400 «Group level visibility has been disabled») |
| Скачивание вложения без токена | 302 на `login.jsp` того же хоста |
| Basic в REST | включён (токены выдаются без браузера) |

## Что стенд не проверяет

- Jira 8.x и 9.x: ключ права, пустой `username` и формы ответов могут
  отличаться — сверить до включения вида для таких версий.
- Каталог LDAP/AD и SSO — пользователи во внутреннем каталоге.
- Настройку «User email visibility» = Hidden/Masked: если Jira не
  отдаёт почты даже администратору, адаптер не выдаст прав никому
  (в журнале — `jira_users_without_email`).
- Держателей «Исполнитель» и «Значение поля-пользователя» — на стенде
  только «Автор задачи» (в поддельном сервере исполнитель есть); все
  они зависят от задачи и спрашиваются по задаче.
- Больше 1000 пользователей на страницу поиска и тысячи задач (цена —
  запрос на задачу в проектах с «Автором задачи» и для задач с уровнем
  безопасности).
- Установку с контекстным путём (`https://host/jira/`) — только на
  поддельном сервере.
