# Живой Seafile (SeafDAV) в Docker

Стенд для проверки вида `seafile` на официальном образе
`seafileltd/seafile-mc`: MariaDB, memcached, TLS-прокси nginx на
127.0.0.1 и выдуманная компания — три пользователя
(`ivan|maria|petr@example.com`), группа `hr` (`maria`, `petr`), у `ivan`
библиотеки «Личное», «Проекты» (расшарена `maria`) и «Кадры» (расшарена
группе), у `maria` — своя «Личное» с файлом по тому же пути. Тесты —
`tests/live/test_seafile_live.py`; без переменных окружения они
пропускаются, в CI не идут.

Проверено 09.10.2026 на **Seafile 13.0.28 CE** (`seafile-mc:13.0-latest`,
MariaDB 10.11.19, memcached 1.6.45, nginx 1.31.6) — итоги внизу.

## Что нужно

- Docker, ~2 ГБ диска на образы, ~1 ГБ памяти.
- Postgres и Redis для тестов синхронизации — как у остального набора.
- При 429 от Docker Hub — зеркало `mirror.gcr.io/seafileltd/seafile-mc:13.0-latest`
  (и `mirror.gcr.io/library/mariadb`, `memcached`) и `docker tag` в имена
  из compose.yaml.

## Как поднять (≈4 минуты)

1. Сертификат стенда — как в `../nextcloud/README.md`, шаг 1
   (`DAV_TLS_DIR`, `DAV_STAND_CA`).

2. Запустить (порт 8446, другой — `SEAFILE_PORT`) и дождаться `"pong"`
   (первый запуск создаёт базы, 1–2 минуты):

   ```bash
   docker compose -f tests/live/seafile/compose.yaml up -d
   curl -s --cacert ~/dav-tls/cert.pem https://127.0.0.1:8446/api2/ping/
   ```

3. Включить SeafDAV (по умолчанию выключен — так и у клиентов: админ
   Seafile включает его в `seafdav.conf`) и перезапустить:

   ```bash
   docker compose -f tests/live/seafile/compose.yaml exec seafile \
     sed -i 's/^enabled = false$/enabled = true/' /shared/seafile/conf/seafdav.conf
   docker compose -f tests/live/seafile/compose.yaml restart seafile
   ```

4. Засеять компанию (только на чистом стенде):

   ```bash
   SEAFILE_URL=https://127.0.0.1:8446/ uv run python -m tests.live.seafile.seed
   ```

5. Тесты:

   ```bash
   SEAFILE_URL=https://127.0.0.1:8446/ \
     uv run pytest tests/live/test_seafile_live.py -q --no-cov
   ```

6. `cli connector-check` — как в `../webdav/README.md`, шаг 5, с
   `--kind seafile --config server=https://127.0.0.1:8446/seafdav/` и
   почтой сотрудника в `login`.

7. Убрать стенд: `docker compose -f tests/live/seafile/compose.yaml down -v`.

Пароли пользователей — `Kronto-Test-1`, администратора
(`admin@example.com`) — `Kronto-Admin-1`; ключ JWT в compose.yaml —
тоже выдуманный, только для стенда.

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `shared_library_is_a_copy_for_each_employee` | Каждый видит свои библиотеки и расшаренные ему (пользователю и через группу); один и тот же файл общей библиотеки — разный `external_id` у каждого (`seafile:u:…`); png не в листинге; библиотека — первая папка пути |
| `every_format_reaches_the_extractor` | docx, pdf, md, txt скачиваются по SeafDAV и разбираются; ссылка — сам файл в SeafDAV |
| `folder_deeper_than_the_limit_stops_the_listing` | 32 уровня от корня SeafDAV обходятся, 33-й — `tree_too_deep` |
| `sync_gives_each_employee_their_own_copy` | Полная синхронизация трёх сотрудников: копия на каждого, кто видит файл, каждая — только ему; правка → `updated` (копия владельца), удаление → `removed` всех копий, снятая шара → копия бывшего получателя удалена |
| `connector_check_records_without_the_password` | `cli connector-check --fetch 2 --record`: пароля в записи нет |
| `wrong_password_is_auth_failed` | неверный пароль → `auth_failed` |
| `wrong_password_expires_only_that_grant` | гаснет только подключение с неверным паролем |

## Итоги 09.10.2026

- 7 из 7 тестов (≈20 секунд).
- Обещание каталога выполняется: общая библиотека — копия на каждого
  сотрудника (сквозного id в SeafDAV нет), права не смешиваются.
- Вход в SeafDAV — почтой и паролем. С Seafile 11 у пользователя
  внутренний id `…@auth.local`, почта — только адрес для входа:
  REST-вызовы групп и шар ждут внутренний id (`seed.user_ids`), адаптеру
  это не мешает.
- SeafDAV по умолчанию выключен — админу Seafile нужно его включить
  (шаг 3); без этого адрес `/seafdav/` отвечает 502 через nginx образа.

## Что стенд не проверяет

- Вход через SSO и «пароль WebDAV» (`ENABLE_WEBDAV_SECRET`).
- Шару подпапки (не всей библиотеки) и библиотеки отделов (Pro).
- Зашифрованные библиотеки.
