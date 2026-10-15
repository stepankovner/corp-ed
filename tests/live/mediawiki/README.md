# Живая MediaWiki в Docker

Стенд для проверки вида `mediawiki` (режим organization) на официальном
образе `mediawiki` (LTS 1.43) с SQLite: две вики и TLS-прокси nginx на
127.0.0.1.

- **Открытая** (порт 8500): читают все, включая анонимов.
- **Закрытая** (порт 8501): без входа не читается ничего, из вошедших —
  только группа `staff` (и администраторы); пространство «Кадры»
  закрыто расширением Lockdown группе `hr`; файлы — через `img_auth.php`.

Выдуманная компания (stand/seed.sh): `Ivan` (staff), `Maria` (staff, hr),
`Petr` (без групп), администратор `Admin`; страницы «Отпуск» (с
шаблоном), «Больничный», перенаправление «Старое название»,
«Компания:Правила», «Справка:Поиск», «Участник:Ivan», «Кадры:Зарплаты»;
файлы «Регламент.txt» и «Схема.png»; бот-пароли `Admin@kronto` (права
`basic` — его вводит админ компании), `Admin@seed` (правки из тестов),
`Petr@kronto`. Тесты — `tests/live/test_mediawiki_live.py`; без
переменных окружения они пропускаются, в CI не идут.

Проверено 15.10.2026 на **MediaWiki 1.43.11** (`mediawiki:1.43`, PHP
8.3.35, SQLite 3.46.1, Lockdown ветки REL1_43, nginx 1.31.6) — итоги
внизу.

## Что нужно

- Docker, ~1 ГБ диска на образы, ~300 МБ памяти.
- Postgres для теста синхронизации — как у остального набора.
- Образ `mediawiki:1.43.11` (`docker pull mediawiki:1.43.11`); при 429
  от Docker Hub — `docker pull mirror.gcr.io/library/mediawiki:1.43.11` и
  `docker tag mirror.gcr.io/library/mediawiki:1.43.11 mediawiki:1.43.11`
  (так же nginx). Другая версия — `MEDIAWIKI_VERSION`.

Пароли (`Kronto-Test-1-…`, бот-пароли `kronto0stand0…`) выдуманные,
только для стенда на 127.0.0.1.

## Как поднять (≈3 минуты)

Все команды — из корня репозитория.

1. Сертификат стенда — как в `../nextcloud/README.md`, шаг 1
   (`DAV_TLS_DIR`, `DAV_STAND_CA`).

2. Расширение Lockdown (GPL, только для стенда; в репозиторий не
   кладётся):

   ```bash
   mkdir -p ~/Lockdown && curl -sSL \
     https://gerrit.wikimedia.org/r/plugins/gitiles/mediawiki/extensions/Lockdown/+archive/refs/heads/REL1_43.tar.gz \
     | tar xz -C ~/Lockdown
   ```

3. Запустить (порты 8500 и 8501, другие — `MEDIAWIKI_PORT`,
   `MEDIAWIKI_PRIVATE_PORT`) и дождаться `healthy` (≈1 минута: установка
   при первом старте):

   ```bash
   MEDIAWIKI_LOCKDOWN_DIR=~/Lockdown \
     docker compose -f tests/live/mediawiki/compose.yaml up -d
   docker compose -p kronto-live-mediawiki ps
   ```

4. Засеять обе вики (только на чистом стенде):

   ```bash
   docker compose -p kronto-live-mediawiki exec wiki bash /stand/seed.sh
   docker compose -p kronto-live-mediawiki exec wiki_private bash /stand/seed.sh
   ```

5. Тесты:

   ```bash
   MEDIAWIKI_URL=https://127.0.0.1:8500/ MEDIAWIKI_PRIVATE_URL=https://127.0.0.1:8501/ \
     uv run pytest tests/live/test_mediawiki_live.py -q --no-cov
   ```

6. `cli connector-check` — как в `../webdav/README.md`, шаг 5, с
   `--stand https://127.0.0.1:8500 -- --kind mediawiki --config
   api_url=https://127.0.0.1:8500/api.php --config "user_emails=Ivan =
   ivan@example.com" --credential username=Admin@kronto --credential
   password=…`. Пока вид не подключён к `default_registry`, команда его не
   знает — тест `connector_check_records_without_the_password` подменяет
   реестр сам.

7. Убрать стенд: `docker compose -p kronto-live-mediawiki down -v`.

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `public_wiki_pages_files_and_readers` | Открытая вики: основное пространство без перенаправления, «Регламент.txt» (png пропущен); права — явный список из сопоставления (Ghost — нет в вики), не «вся компания»; HTML из `action=parse` с развёрнутым шаблоном и без отчёта парсера |
| `pages_are_continued` | Продолжение списков (`continue` → `gapcontinue`) по страницам из двух: те же страницы, что одним запросом |
| `listed_namespaces_by_name` | Пространства из настройки по названию («основное», «Компания», канонический «Help»), путь «Компания/Справка» |
| `private_wiki_with_lockdown_needs_namespaces` | Lockdown виден в `siprop=extensions` → без списка пространств `namespaces_required` |
| `private_wiki_readers_without_img_auth_files` | Закрытая вики: читатели — ivan и maria (у petr нет `read` по `usprop=rights`), «Кадры» не взяты, файлы за img_auth.php не листятся |
| `img_auth_refuses_the_bot_password_session` | С той же сессией бот-пароля api.php отвечает, img_auth.php — 403 |
| `private_wiki_is_closed_without_login` | Без входа — `readapidenied` и 403 на файл |
| `bot_without_read_right_is_refused` | Бот-пароль учётки без права чтения → `AdapterAuthError("read_denied")` |
| `wrong_bot_password_is_an_auth_error` | Неверный бот-пароль → `unauthorized` |
| `sync_mirrors_readers_and_follows_changes` | Синхронизация через ядро: каждый видит ровно то, что вики; правка → `updated`, удаление → `removed`, блокировка Petr снимает доступ без новой версии |
| `connector_check_records_without_the_password` | `cli connector-check --fetch 2 --record`: ни бот-пароля, ни cookie сессии в записи |

## Итоги 15.10.2026

- 11 из 11 тестов (≈6 секунд), повторный прогон — тоже 11 из 11
  (тест синхронизации возвращает стенд в исходное состояние).
- **Найдено и учтено:** сессия бот-пароля действует только в api.php и
  rest.php (`BotPasswordSessionProvider`), а img_auth.php отвечает на неё
  403 — файлы закрытой вики с img_auth не скачать по бот-паролю, а
  содержимого файлов Action API не отдаёт. Адаптер такие файлы не
  листит (журнал `mediawiki_files_behind_img_auth`), а не падает на
  каждом. Без img_auth (файлы отдаёт веб-сервер) модуль «Файлы»
  работает и на закрытой вики.
- `usprop=rights` в сессии бот-пароля показывает права пользователя в
  пересечении с правами бот-пароля (у `basic` — `read` и немногое
  другое); для решения «читает ли» этого достаточно, `read` в `basic`
  есть.
- Ошибки API приходят с HTTP 200 (`readapidenied`, `assertuserfailed`,
  `nosuchpageid`); `assert=user` на истёкшую сессию — `assertuserfailed`.
- Учётка с правом чтения (администратор) видит «Кадры» под Lockdown, и
  `intestactions=read` для неё — `true`: по ответам API закрытость
  пространства не видна, поэтому без списка пространств — отказ.

## Что стенд не проверяет

- OAuth (owner-only consumer): расширения OAuth нет в поставке.
- Вики на MySQL/MariaDB и PostgreSQL (адаптеру всё равно, API тот же),
  вики за SSO (PluggableAuth, SAML) — бот-пароль там обычно работает,
  но не проверялся.
- Файлы на отдельном домене (`$wgUploadPath` на CDN) — не берутся по
  коду, живьём не воспроизводили.
- Большие вики (тысячи страниц, `maxlag` на репликах) — только на
  поддельном сервере.
