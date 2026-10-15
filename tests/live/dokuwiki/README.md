# Живая DokuWiki в Docker

Стенд для проверки вида `dokuwiki` (режим organization) на официальном
образе `dokuwiki/dokuwiki`: TLS-прокси nginx на 127.0.0.1 и выдуманная
компания — суперпользователь `admin` (группа `admin`, владелец токена),
`ivan` (группа `sales`), `maria` и `petr` (группа `hr`), `olga` (только
`user`). ACL (`conf/acl.auth.php`): вошедшие читают всё; `public` —
всем, включая анонимов; `hr` — только группе `hr`, и в нём страница
`hr:salaries` запрещена `petr`; `sales` — только группе `sales`.
Страницы и два docx в `hr` и `public` засевает `seed.py`. Тесты —
`tests/live/test_dokuwiki_live.py`; без переменных окружения они
пропускаются, в CI не идут.

Проверено 15.10.2026 на **DokuWiki 2026-07-14c «Mort»**
(`dokuwiki/dokuwiki:2026-07-14c`, PHP 8.4.26; nginx 1.31.6), API
версии 14 — итоги внизу.

## Что нужно

- Docker, ~0,5 ГБ диска на образ, ~200 МБ памяти.
- Postgres и Redis для теста синхронизации — как у остального набора.
- При 429 от Docker Hub — `docker pull mirror.gcr.io/dokuwiki/dokuwiki:2026-07-14c`
  и `docker tag` в имя из compose.yaml (так же для nginx).

Пароль пользователей (`Kronto-Test-1`, в `conf/users.auth.php` — его
bcrypt) — выдуманный, только для стенда на 127.0.0.1.

## Как поднять (≈1 минута)

Все команды — из корня репозитория.

1. Сертификат стенда — как в `../nextcloud/README.md`, шаг 1
   (`DAV_TLS_DIR`, `DAV_STAND_CA`).

2. Запустить (порт 8490, другой — `DOKUWIKI_PORT`). Настройки
   (`conf/local.php`: ACL, authplain, суперпользователи `@admin`,
   `remote=1`, `remoteuser=@user`), пользователи и ACL копируются в том
   при первом старте — установщик не нужен:

   ```bash
   docker compose -f tests/live/dokuwiki/compose.yaml up -d
   curl -s --cacert ~/dav-tls/cert.pem -u admin:Kronto-Test-1 \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"core.whoAmI","params":{}}' \
     https://127.0.0.1:8490/lib/exe/jsonrpc.php
   ```

3. Токены. В жизни токен берут в профиле («Обновить профиль» → «Токен
   авторизации»); на стенде — тем же кодом DokuWiki из контейнера
   (`admin` — владелец ключа, `ivan` — обычный пользователь, для проверки,
   что такой ключ не принимается):

   ```bash
   for u in admin ivan; do
     docker compose -f tests/live/dokuwiki/compose.yaml exec -T -u www-data dokuwiki \
       php -r 'define("DOKU_INC", "/var/www/html/"); require DOKU_INC . "inc/init.php";
               echo \dokuwiki\JWT::fromUser($argv[1])->getToken();' "$u" > ~/dokuwiki-$u.token
   done
   ```

4. Засеять страницы и файлы (повторный запуск безопасен):

   ```bash
   DOKUWIKI_URL=https://127.0.0.1:8490/ uv run python -m tests.live.dokuwiki.seed
   ```

5. Тесты:

   ```bash
   DOKUWIKI_URL=https://127.0.0.1:8490/ DOKUWIKI_TOKEN="$(cat ~/dokuwiki-admin.token)" \
   DOKUWIKI_MEMBER_TOKEN="$(cat ~/dokuwiki-ivan.token)" \
     uv run pytest tests/live/test_dokuwiki_live.py -q --no-cov
   ```

6. `cli connector-check` — как в `../webdav/README.md`, шаг 5, с
   `--stand https://127.0.0.1:8490 -- --kind dokuwiki --config
   base_url=https://127.0.0.1:8490/ --config "users=admin
   admin@example.com; ivan ivan@example.com; …" --credential token=…`
   (после подключения вида к `default_registry`; до того тест
   подставляет свой реестр).

7. Убрать стенд: `docker compose -f tests/live/dokuwiki/compose.yaml
   down -v` и `docker image rm dokuwiki/dokuwiki:2026-07-14c`.

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `listing_mirrors_dokuwiki_acl` | Права из `core.aclCheck` для каждого логина списка: `public` — все, `hr` — группа hr и суперпользователь, `hr:salaries` — без `petr` (запрет на страницу), `sales` — группа sales; файлы — по пространству; документы только RESTRICTED; заголовки (первый заголовок страницы), ссылки `doku.php?id=…`, размер файла |
| `grouping_by_namespace_matches_per_item_checks` | Проверка один раз на пространство (по таблице `plugin.acl.listAcls`) даёт ровно то же, что `aclCheck` на каждый документ — на самой DokuWiki |
| `namespaces_narrow_the_walk` | Поле «Пространства» (`hr`) — только документы `hr` с теми же правами |
| `login_and_password_work_too` | Вход логином и паролем (Basic) даёт тот же листинг |
| `documents_come_as_html_and_files` | Страница — HTML `core.getPageHTML` с текстом и заголовком; файл — байты `core.getMedia` совпадают с загруженными |
| `wrong_token_is_an_auth_error` | Неверный токен и неверный пароль → `AdapterAuthError("unauthorized")` (DokuWiki отвечает 401) |
| `member_token_is_refused` | Токен не суперпользователя → `admin_required` |
| `sync_mirrors_access_and_follows_changes` | Полная синхронизация: каждый сотрудник kronto видит ровно то, что DokuWiki; правка страницы → `updated`, удалённые страница и файл → `removed`, снятый запрет (`plugin.acl.delAcl`) даёт доступ без новой ревизии; docx разбирается |
| `connector_check_records_without_the_token` | `cli connector-check --fetch 2 --record`: токена и пароля в записи нет |

## Итоги 15.10.2026

- 9 из 9 тестов (≈20 секунд).
- Формы ответов совпали с поддельным сервером, кроме одного: уровни в
  `plugin.acl.listAcls` приходят строками (`"8"`) — адаптер берёт из
  таблицы только ключи; поддельный сервер поправлен.
- Новая вики содержит страницы `wiki:*` и `playground:playground` из
  образа — они индексируются с правами корня (`@user` — все из списка).
- Листинг стенда: 10 страниц и 2 файла (картинки из образа пропущены
  как неподдерживаемый формат), 35 вызовов `core.aclCheck` — 7 групп
  (пространства и `hr:salaries`) × 5 логинов.
- Подстановки подтверждены вручную: с правилами `people:* @ALL 0` и
  `people:%USER%:* %USER% 8` таблица от имени admin содержит
  `people:admin:* admin 8`, а `aclCheck("people:ivan:x", "ivan")` = 0,
  хотя сам ivan в DokuWiki там пишет, — личные пространства сотрудникам
  не достаются (в сторону закрытости, описано в адаптере).
- `core.savePage` сразу индексирует страницу, поэтому разница между
  листингом по индексу (`depth=0`) и по файлам на стенде не видна;
  адаптер всё равно берёт файлы — страницы, положенные в `data/pages`
  мимо редактора, в индекс сами не попадают.

## Что стенд не проверяет

- DokuWiki до Kaos (2024-02-06): нового API нет — адаптер отвечает
  `dokuwiki_too_old` (по коду, не живьём).
- Бэкенды авторизации кроме authplain (LDAP, AD) и плагины, которые
  меняют проверку прав (событие AUTH_ACL_CHECK).
- Правила с подстановками `%USER%`/`%GROUP%` — ограничение описано в
  адаптере.
- Связки веб-сервера, которые не передают Authorization в PHP: токен
  дублируется в `X-DokuWiki-Token`, живьём не воспроизводили.
