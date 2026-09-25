# Развёртывание corp-ed

Как поднять и обслуживать систему в бою. Что и почему устроено именно
так — в `DECISIONS.md`; что известно и не закрыто — в `RISKS.md`;
модель угроз и проверки безопасности — в `SECURITY.md`.

---

## 1. Из чего состоит

| Сервис | Образ | Что делает |
|---|---|---|
| `api` | `corp-ed` | HTTP API (uvicorn), порт 8000 — только для reverse proxy |
| `worker` | `corp-ed` | фоновый ингест: `python -m corp_ed.worker` |
| `migrate` | `corp-ed` | одноразово при старте: `alembic upgrade head` |
| `db` | `pgvector/pgvector:pg16` | PostgreSQL + pgvector, единственное хранилище данных |
| `redis` | `redis:7-alpine` | лимиты частоты и квота эмбеддингов; без диска, без пароля не стартует |
| cron на хосте | `corp-ed` | раз в сутки `cli purge` и `cli gaps --all` |

Один образ на всё: API, воркер, миграции, CLI. Код и окружение внутри
принадлежат root и доступны только на чтение; процессы работают под
пользователем `app` без shell.

Перед API обязателен reverse proxy с TLS (nginx, Caddy, балансировщик
облака). Приложение само TLS не терминирует.

---

## 2. Две роли в базе

| Роль | Кто | Права |
|---|---|---|
| `corp_ed` (`POSTGRES_USER`) | миграции (`migrate`), администратор | владелец схемы, суперпользователь образа postgres |
| `corp_ed_app` | `api`, `worker`, CLI | `SELECT/INSERT/UPDATE/DELETE`, без DDL, `NOSUPERUSER NOBYPASSRLS` |

Зачем две: Row-Level Security с `FORCE` действует на владельца таблиц,
но **не** на суперпользователя. Приложение под суперпользователем
обошло бы третье кольцо изоляции молча. В `production` приложение
отказывается стартовать под ролью с `SUPERUSER` или `BYPASSRLS`.

Роль `corp_ed_app` создаёт `deploy/postgres/10-app-role.sh` при первой
инициализации тома (смонтирован в `/docker-entrypoint-initdb.d`).
Пароль — `APP_DB_PASSWORD`. Права на будущие таблицы выданы через
`ALTER DEFAULT PRIVILEGES` — они действуют на таблицы, которые создаёт
`corp_ed`, поэтому миграции должны идти именно под ним
(`MIGRATIONS_DATABASE_URL`).

Если том уже инициализирован без скрипта (база поднята раньше):

```bash
docker compose -f compose.yaml exec -e APP_DB_PASSWORD='…' db \
    sh /docker-entrypoint-initdb.d/10-app-role.sh
```

---

## 3. Переменные окружения

Полный список с комментариями — `.env.example`. Обязательное в бою:

| Группа | Переменные | Примечание |
|---|---|---|
| Режим | `ENVIRONMENT=production` | включает HSTS, выключает `/docs`, требует явных хостов, CORS и Redis |
| Секреты | `SECRET_KEY` (≥ 32 символов), `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `REDIS_PASSWORD`, `YC_API_KEY` | генерировать: `openssl rand -hex 32` |
| Yandex Cloud | `YC_FOLDER_ID`, `LLM_PROVIDER`, `LLM_MODEL`, `LLM_MAX_CONCURRENCY`, `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `EMBEDDING_QUERY_RPS`, `EMBEDDING_INGEST_RPS` | квоты каталога делятся между API и воркером |
| Поиск и ответ | `RAG_*` | значения задаёт ML; дефолтов нет намеренно |
| Отчёт о пробелах | `GAPS_CLUSTER_DISTANCE`, `GAPS_HALF_LIFE_DAYS` | значения ML; пороги полнотекста — после подбора на живых логах |
| HTTP-периметр | `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `FORWARDED_ALLOW_IPS` | см. раздел 5 |
| Кредиты | `BILLING_*` | дефолты — предложение досье, пересмотреть с тарифами |
| Коннекторы | `CONNECTOR_SECRETS_KEYS` (обязателен в `production`), `CONNECTOR_*` | ключ Fernet: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`; несколько через запятую — ротация (раздел 9) |

`.env` лежит рядом с `compose.yaml`, права `600`, в репозиторий не
попадает (`.gitignore`). Секреты в переменных окружения видны в
`docker inspect` тому, у кого есть доступ к демону Docker, — это
принятая граница: доступ к хосту равен доступу ко всему.

---

## 4. Первый запуск

```bash
cp .env.example .env            # заполнить, см. раздел 3
docker compose -f compose.yaml up -d --build
docker compose -f compose.yaml logs migrate   # alembic до head, контейнер завершился с 0
docker compose -f compose.yaml ps             # api, worker healthy
```

`docker compose -f compose.yaml` — именно так: без `-f` compose добавит
`compose.override.yaml` с настройками разработки (`--reload`, монтирование
исходников, открытый порт базы).

Первую компанию заводит команда из CLI — HTTP-ручки для этого нет
намеренно (решение в `DECISIONS.md`):

```bash
docker compose -f compose.yaml run --rm api python -m corp_ed.cli create-tenant \
    --code acme --name "ACME" --seats 50 --admin-email admin@acme.ru
```

Временный пароль администратора печатается один раз. Передать его
клиенту отдельным каналом от кода компании; при первом входе система
потребует сменить пароль.

Остальные команды: `set-seats`, `set-not-found-mode`, `suspend-tenant`,
`resume-tenant`, `reindex`, `purge`, `gaps` — `python -m corp_ed.cli --help`.

---

## 5. Reverse proxy

Требования к прокси перед `api:8000`:

- **TLS** терминируется на прокси. HSTS приложение отдаёт само в
  `production`.
- **`X-Forwarded-For`** и `X-Forwarded-Proto` прокси ставит сам, а
  входящие от клиента — перезаписывает. uvicorn запущен с
  `--proxy-headers` и верит этим заголовкам только от адресов из
  `FORWARDED_ALLOW_IPS` (адрес прокси). Без этого лимиты частоты и
  журнал аудита видели бы адрес прокси или подделанный заголовок.
- **`ALLOWED_HOSTS`** — боевое имя плюс `127.0.0.1` (HEALTHCHECK
  контейнера идёт через ту же проверку Host).
- **Лимит тела** на прокси не меньше `MAX_UPLOAD_BYTES` (25 МБ по
  умолчанию), иначе загрузка файлов обрезается прокси с невнятной
  ошибкой. Приложение свои лимиты применяет само (1 МБ JSON, 25 МБ файл).
- **Таймауты** чтения ответа — не меньше 60 с: ответ модели плюс
  эмбеддинг вопроса.
- Порт 8000 наружу не публиковать; в `compose.yaml` он привязан к хосту
  для прокси на том же хосте — при прокси в другой сети заменить на
  внутреннюю сеть compose.

---

## 6. Регулярные задачи

Cron на хосте (или systemd timer), под пользователем с доступом к Docker:

```cron
# Удалить журнал вопросов старше QA_LOG_RETENTION_DAYS и аудит старше года.
10 3 * * *  cd /opt/corp-ed && docker compose -f compose.yaml run --rm api python -m corp_ed.cli purge
# Пересобрать отчёт о пробелах по всем активным компаниям (после purge).
30 3 * * *  cd /opt/corp-ed && docker compose -f compose.yaml run --rm api python -m corp_ed.cli gaps --all
```

`purge` удаляет и журнал запусков коннекторов старше
`CONNECTOR_SYNC_RUN_RETENTION_DAYS` (90). Синхронизация коннекторов по
расписанию — внутри `worker`, отдельной задачи cron не нужно.

`gaps` держит advisory-блокировку: параллельный запуск завершится с
ошибкой, а не построит отчёт дважды. Ненулевой код выхода — ошибка
хотя бы у одной компании, подробности в логе.

---

## 7. Обновление

```bash
git pull
docker compose -f compose.yaml up -d --build
```

`api` и `worker` стартуют только после успешного `migrate`
(`service_completed_successfully`). Миграции пишутся совместимыми с
предыдущей версией кода там, где это возможно; там, где нет (смена
размерности векторов), — переиндексация ставится в очередь самой
миграцией, а поиск возвращает пустую выдачу, пока воркер не догонит.

Откат: `docker compose -f compose.yaml run --rm migrate alembic downgrade -1`
и предыдущий образ. Даунгрейды проверяются в CI на пустой базе; на
живой базе — только после бэкапа.

---

## 8. Бэкапы и восстановление

Единственное состояние — PostgreSQL. Redis восстанавливать нечего
(счётчики лимитов).

```bash
docker compose -f compose.yaml exec db pg_dump -U corp_ed -Fc corp_ed > corp_ed-$(date +%F).dump
```

Раз в сутки, хранить не меньше 30 дней вне хоста. Дамп содержит
документы компаний и персональные данные — шифровать при передаче и
хранении, доступ как к самой базе.

Восстановление проверяется, а не предполагается: раз в квартал
поднять дамп в пустой базе и прогнать `alembic current` и вход
пользователя.

---

## 9. Секреты и их ротация

| Секрет | Что происходит при ротации |
|---|---|
| `SECRET_KEY` | все access-токены (15 мин) становятся недействительными; refresh-токены не подписываются ключом, сессии переживают смену — пользователи получат новый access при следующем обновлении |
| `YC_API_KEY` | заменить в `.env`, перезапустить `api` и `worker`; ключ нигде не логируется и не хранится в базе |
| `APP_DB_PASSWORD` | `ALTER ROLE corp_ed_app PASSWORD '…'`, затем `.env` и перезапуск |
| `REDIS_PASSWORD` | `.env`, перезапуск `redis`, `api`, `worker` |
| `CONNECTOR_SECRETS_KEYS` | новый ключ дописать **первым** через запятую, перезапустить `api` и `worker` (новые записи шифруются им, старые читаются вторым), выполнить `cli rotate-connector-secrets`, затем убрать старый ключ и перезапустить снова. Потеря всех ключей = все подключения останавливаются с `credentials_unreadable`, учётные данные вводятся заново |

Утечка любого секрета — повод для ротации в тот же день, а не для
расследования сначала. «Выйти везде» для одного пользователя —
`POST /auth/logout-all`; блокировка компании — `cli suspend-tenant`
(токены перестают приниматься сразу).

---

## 9a. Исходящий трафик воркера

Воркер ходит в системы клиентов по адресам из настроек коннекторов.
В коде адреса проверяются (`core/outbound.py`: только `https`,
публичные IP, закрепление адреса), но на стенде стоит добавить вторую
линию: сетевая политика для контейнера `worker` — запрет исходящих
соединений к `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
`169.254.0.0/16`, `127.0.0.0/8` и к внутренним сервисам, кроме базы и
Redis (например, правила `iptables`/`nftables` на docker-сети или
egress-политика оркестратора). API наружу ходит только в Yandex Cloud
и, для проверки учётных данных (`POST /connectors/{id}/test`), к тем
же адресам систем клиентов.

## 10. Наблюдение

- Логи — JSON в stdout (`docker compose logs`), с `request_id`; секреты
  и пароли вырезаются процессором structlog. Собирать во внешнюю
  систему с хранением ≥ 90 дней — журнал аудита в базе хранится год, но
  логи с `request_id` нужны для разбора инцидента.
- `GET /health` — живость `api` (без базы и Redis). Сбой базы виден по
  500 на боевых ручках и по логу `internal_error`.
- Воркер: HTTP-порта нет; признак остановки — растущее число задач
  `ingest_jobs` в статусе `PENDING` и материалы, не переходящие в
  `READY`. Застрявшие `RUNNING` воркер сам переоткрывает через 15 минут.
- Аудит: `GET /api/v1/audit` для администратора компании; события
  `auth.login.failed`, `auth.refresh.reuse_detected`, `credits.*` —
  сигналы, на которые стоит смотреть команде.

---

## 11. Чек-лист перед выдачей адреса клиенту

- [ ] `ENVIRONMENT=production`, приложение стартовало (иначе оно
      падает с внятной причиной: `*` в CORS, нет Redis, суперпользователь).
- [ ] `/docs` и `/openapi.json` отвечают 404.
- [ ] `curl -I https://…/health` показывает `Strict-Transport-Security`,
      `X-Content-Type-Options`, `Content-Security-Policy`, нет `Server`.
- [ ] Запрос с чужим `Host` получает 400.
- [ ] `psql -U corp_ed_app -c 'CREATE TABLE t(i int)'` — отказ.
- [ ] Бэкап снят и восстановлен в тестовой базе хотя бы раз.
- [ ] Cron `purge` и `gaps` стоит и отработал вручную.
- [ ] Прогон `security.yaml` на текущем коммите зелёный.
