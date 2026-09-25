# Архитектура corp-ed

Карта системы на 25 сентября 2026. Описывает то, что существует в коде,
а не то, что запланировано. Почему так — `DECISIONS.md`; что не закрыто —
`RISKS.md`; меры защиты и как их проверить — `SECURITY.md`; как поднять —
`DEPLOY.md`; сверка с контрактом ML — `INTEGRATION.md`.

Продукт: ассистент по внутренним документам компании. Администратор
компании загружает документы, сотрудники задают вопросы и получают ответ
со ссылками на выдержки; администратор видит, каких документов не хватает.

---

## 1. Общая форма

Слоистая архитектура с однонаправленной зависимостью:

```
api  →  services  →  repositories  →  domain
```

Вне цепочки:

- **`core`** — сквозная инфраструктура: конфиг, база и хуки изоляции,
  политики базы (RLS, триггер аудита), безопасность, исключения,
  логирование, middleware, лимиты частоты, контекст запроса и тенанта.
- **`llm`** — адаптеры к Yandex Cloud (генерация, эмбеддинги), контракт,
  ретраи, ограничитель квоты. Уровень `repositories`: переводит с языка
  внешней системы на язык домена.
- **`ingest`** — извлечение текста из файлов и его песочница.
- **`connectors`** — адаптеры к системам-источникам документов
  (интерфейс, каталог видов, очистка HTML). Как `llm`: только
  ввод-вывод, без базы и без знания о компании.
- **`prompts`** — промпты (ML).
- Точки входа: `main.py` (API), `worker.py` (фоновый ингест и
  синхронизация коннекторов), `cli.py` (команды команды Kronto).

Правило целостности: **импорт вверх по цепочке — протечка слоя.**
`domain` не знает про `sqlalchemy`-сессии, `httpx` и FastAPI; `core` не
импортирует `domain` (настройка `retriever` в `core` — `Literal`, enum
живёт в `domain`, преобразование — в композиционном корне).

Разделение с ML (Артём): ML пишет **чистые функции** — нарезка,
бюджет контекста, слияние выдач, запрос полнотекста, расширение
словарём, классификация и кластеризация пробелов, маскирование ПДн,
промпты и eval. Бэкенд встраивает их, владеет сетью, базой,
транзакциями и API. Файлы ML бэкенд не редактирует.

---

## 2. Слои

### 2.1. `api/v1` — граница с клиентом

Единственный слой, знающий про HTTP. Тела запросов — Pydantic-схемы с
`extra="forbid"` (`schemas/base.py`); ответы — явные схемы, внутренние
поля наружу не уходят. Исключения в HTTP-коды превращают обработчики,
зарегистрированные в `main.py` (3.5).

| Префикс | Ручки | Роль |
|---|---|---|
| `/auth` | `POST login`, `POST refresh`, `POST logout`, `POST logout-all`, `POST change-password`, `GET me` | вход — без токена; остальное — любой (с временным паролем — только `change-password`, `me`) |
| `/users` | `GET`, `POST`, `PATCH {id}`, `POST {id}/reset-password` | ADMIN |
| `/materials` | `GET`, `POST` (текст), `POST upload` (файл), `GET {id}`, `POST {id}/ingest`, `PATCH {id}`, `DELETE {id}` | ADMIN |
| `/faq` | `POST ask`, `POST search`, `PATCH answers/{id}` | `ask` и оценка — любой; `search` — ADMIN |
| `/glossary` | `GET`, `POST`, `PATCH {id}`, `DELETE {id}` | ADMIN |
| `/gaps` | `GET`, `PATCH {id}` | ADMIN |
| `/usage` | `GET` | ADMIN |
| `/audit` | `GET` | ADMIN |
| `/health`, `/` | `GET` | без токена |

`dependencies.py` — композиционный корень: собирает репозитории,
сервисы, адаптеры и раздаёт через `Depends`. `get_current_user` —
единственное место, где тенант попадает в контекст, и он берётся из
подписанного токена. `require_role(*roles)` — фабрика зависимостей.
`rate_limits.py` — политики лимитов и зависимости `limit_by_ip/user/tenant`.

Ручек создания компании нет намеренно: компании заводит команда из CLI.

### 2.2. `services` — бизнес-логика

| Сервис | Отвечает за |
|---|---|
| `AuthService` | вход, ротация refresh с обнаружением повторного использования, выход, «выйти везде», смена пароля |
| `UserService` | пользователи компании, сброс пароля, защита последнего администратора |
| `TenantService` | компании: заведение с первым администратором, приостановка, места, режим «ответа нет» (только CLI) |
| `MaterialService` | документы: текст и файл (проверки, песочница), дубликаты по sha256, переименование с переиндексацией, удаление; ставит задачу воркеру |
| `IngestService` | нарезка → эмбеддинги → чанки одного материала (в воркере) |
| `ReindexService` | постановка всех материалов компании/всех компаний в очередь |
| `FaqService` | ответ: пул кредитов → словарь → эмбеддинг → поиск (вектор или гибрид) → порог → промпт → пометка/отказ → `qa_log`; оценка; отладочный поиск |
| `CreditService` | пул кредитов компании за месяц, остановка до платных вызовов, пороги 80/100 % |
| `GlossaryService` | словарь сокращений компании |
| `GapService` | отчёт о пробелах для админа, статус |
| `GapReportService` | ночная пересборка отчёта (классы → кластеры → сопоставление со вчерашними → подпись → запись) |
| `RetentionService` | удаление по срокам хранения |

Сервисы получают зависимости в конструктор, коммитят транзакцию сами;
репозитории только `flush`. Внешние вызовы (модель, эмбеддинги, дочерний
процесс) — вне открытой транзакции там, где это возможно.

### 2.3. `repositories` — доступ к данным

По репозиторию на агрегат: `tenant`, `user`, `refresh_token`, `material`,
`chunk` (векторный и полнотекстовый поиск), `ingest_job` (очередь с
`FOR UPDATE SKIP LOCKED`), `qa_log`, `glossary`, `gap`, `audit`.

Правила: `get_by_id` — через `select`, не `session.get` (identity map
обходит фильтр тенанта); колоночные и массовые запросы фильтруют тенанта
явно (`require_tenant()`), потому что ORM-хук их не видит.

### 2.4. `domain` — модели и чистые функции

Таблицы (все тенантские — с `TenantMixin` и под RLS, кроме отмеченных):

| Сущность | Назначение |
|---|---|
| `Tenant` (не тенантская) | компания: код, активность, `seats`, `not_found_mode` |
| `User` | сотрудник или администратор; `token_version`, `must_change_password` |
| `RefreshToken` (не тенантская) | sha256 токена, семейство, отзыв |
| `Material` | документ: текст, источник (имя, формат, размер, sha256), статус индексации |
| `Chunk` | выдержка: `heading_path`, `embed_text`, `content`, вектор 768, `fts` (генерируемая) |
| `IngestJob` | задача воркера: попытки, статус, ошибка |
| `QaLog` | вопрос (маскированный), его вектор, модели, версия промпта, сигналы поиска, `origin`, источники, токены, кредиты, оценка, `miss_kind` |
| `GlossaryTerm` | сокращение → расшифровка |
| `GapCluster`, `GapClusterQuestion` | пробел и его вопросы |
| `AuditEvent` (не тенантская) | запись аудита, только дописывается |

Модули ML в `domain`: `split` (нарезка с крошками), `context` (бюджет
токенов), `fusion` (RRF), `fulltext` (строка запроса), `query`
(расширение словарём), `gaps` (классы промахов, кластеризация,
приоритет, `mask_pii`), `tokens`. Бэкенд: `types` (`ChunkMatch`,
`FaqAnswer`, `AnswerOrigin`, `NotFoundMode`, `Retriever`, `GapStatus`),
`credits` (стоимость и период).

---

## 3. `core`

### 3.1. Конфигурация

Классы настроек читаются лениво там, где создаются объекты:

| Класс | Префикс | Что |
|---|---|---|
| `Settings` | — | окружение, `SECRET_KEY` (≥ 32, `SecretStr`), TTL токенов, база, срок `qa_log` |
| `LLMSettings` | `YC_*`, `LLM_*`, `EMBEDDING_*` | провайдер и модель, семафор, эмбеддер, доли квоты |
| `RagSettings` | `RAG_*` | нарезка, лимит, порог, бюджет, температура, способ поиска — **без дефолтов**, значения за ML |
| `GapsSettings` | `GAPS_*` | отчёт о пробелах: значения ML без дефолтов, инженерные потолки с дефолтами |
| `BillingSettings` | `BILLING_*` | кредиты: предложение досье |
| `HttpSettings` | — | CORS, хосты, Redis, лимиты тела; в `production` — обязательные проверки |

`EMBEDDING_DIM = 768` — константа схемы: настройка обязана ей равняться.

### 3.2. База данных и изоляция

`get_engine`/`get_session_maker` — один движок на процесс. Три хука
(`core/database.py`): `after_begin` ставит GUC `app.tenant_id`
(`set_config(..., true)` — на транзакцию), `do_orm_execute` подмешивает
фильтр тенанта в ORM-select и синхронизирует GUC, `before_flush`
проверяет `tenant_id` записываемых объектов. `tenant_context.py`:
`current_tenant`, `require_tenant()`, `tenant_scope()` с обязательным
сбросом.

`db_policies.py` — SQL, которого нет в моделях: политики RLS с `FORCE`
для `TENANT_TABLES`, триггер неизменяемости аудита. Один источник для
миграций и для тестовой схемы. Здесь же задокументирована ловушка:
под FORCE владелец-несуперпользователь не видит строк в миграции —
массовые правки идут через `NO FORCE`/`FORCE` или `TRUNCATE`.

### 3.3. Безопасность

`security.py`: argon2id с пустышкой для одинакового времени, access JWT
с полным набором claims, непрозрачный refresh и его хеш, временный
пароль. `password_policy.py`: правила пароля. `rate_limit.py`:
`RateLimiter` (Redis Lua / память), `RateLimitedError`,
`RateLimiterUnavailableError`. `middleware.py`: `RequestIDMiddleware`
(идентификатор, адрес клиента, перехват необработанных исключений в
JSON 500), `SecurityHeadersMiddleware`, `BodySizeLimitMiddleware` (413 до
чтения тела). `logging.py`: structlog, `redact_sensitive`.

### 3.4. Исключения

```
DomainError (400)
├── ConflictError (409) ── DuplicateMaterialError (+ material_id)
├── NotFoundError (404)
├── PermissionError (403) ── PasswordChangeRequiredError
├── InvalidCredentialsError (401, единый текст)
├── NotAuthenticatedError (401 + WWW-Authenticate)
├── WeakPasswordError (422)
├── UnacceptableFileError (415 | 422 + code)
├── CreditsExhaustedError (402 + code)
├── ServiceUnavailableError (503)
├── LastAdminError, SelfModificationError, InvalidSeatsError, …
└── LLMError (502, retryable) — в llm/errors.py
Отдельно: TenantContextMissingError, TenantMismatchError → 500 без текста
          RateLimitedError → 429 + Retry-After
          RequestValidationError → 422 без эха входа
```

### 3.5. Жизненный цикл (`main.py`)

`lifespan`: `SELECT 1` и проверка роли базы (в `production` —
`SUPERUSER`/`BYPASSRLS` = отказ), Redis (`ping`) → лимитер и ограничитель
квоты эмбеддингов (или память вне `production`), семафор LLM,
`httpx.AsyncClient`. Докс выключены в `production`.

Middleware, снаружи внутрь: `CORS` → `SecurityHeaders` → `RequestID` →
`TrustedHost` → `BodySizeLimit`. Порядок важен: заголовки безопасности и
`request_id` есть и у 400 от `TrustedHost`, и у 413.

---

## 4. `llm` и `ingest`

- `types.py`, `gateway.py` (`LLMGateway.generate(messages, temperature,
  max_tokens, response_format)`), `embedding_gateway.py`.
- `errors.py`, `retry.py` — классификация и экспоненциальные повторы.
- `yandex.py` (нативный API), `yandex_openai.py` (OpenAI-совместимый,
  Alice AI LLM Flash) — выбираются `factory.build_llm_gateway` по
  `LLM_PROVIDER`; оба принимают семафор и `response_format` (строгий
  JSON). `yandex_embedding.py` — `text-embeddings-v2`, dim 768, отдельные
  ограничители для вопросов и документов. `throttle.py` — слоты квоты в
  Redis (Lua + Redis TIME) с честным разделением долей.
- `fake.py`, `fake_embedding.py` — для тестов.
- `ingest/extract.py` — сигнатуры, zip-бомба, docx (mammoth →
  markdownify), pdf (pymupdf4llm, страницы через `\f`); `sandbox.py` —
  дочерний `python -I`, чистое окружение, таймаут, бюджет CPU = таймаут ×
  ядер (разбор многопоточный), убийство по лимиту → `timeout`;
  `extract_worker.py` — rlimits; `preprocess.py` (ML) — чистка Markdown.
- `connectors/base.py` — `SourceAdapter` (`check`, `list`, `fetch`),
  `RemoteDocument` (id, версия, ссылка, права словами источника),
  `FetchedFile | FetchedPage`, `AdapterError`/`AdapterAuthError`;
  `registry.py` — `KindSpec` (режим, модули, поля формы и учётных
  данных, поле адреса) и `AdapterRegistry` (фабрики адаптеров);
  `html.py` — очистка HTML страниц до Markdown. Сеть — только через
  `core/outbound.py::OutboundClient` (проверка адреса и закрепление IP; за
  egress-прокси — по имени, `CONNECTOR_OUTBOUND_VIA_PROXY`, `DEPLOY.md` §9a).
  Адаптеры конкретных систем добавляются этапами: Битрикс24 →
  Confluence → Яндекс 360.

---

## 5. Основные потоки

**Вопрос сотрудника** (`POST /faq/ask`):
токен → тенант в контекст → лимит частоты → `CreditService.ensure_available`
(402 до платных вызовов) → режим компании → `expand_query` словарём (только
для поиска) → эмбеддинг вопроса (слот квоты) → поиск: `vector` (top-K,
порог на каждой выдержке) или `hybrid` (вектор ∪ полнотекст по 50, RRF,
порог по лучшему вектору) → `select_context` по бюджету токенов →
`build_faq_messages` → модель (семафор) → `normalize_citations` → если
отказ или пусто: общий ответ без выдержек с `ensure_general_prefix`
(или `NOT_FOUND_ANSWER` в строгом режиме) → строка `qa_log` + пороги
кредитов в одной транзакции → ответ с `origin`, `sources`, `answer_id`,
`diagnostics` (ADMIN).

**Документ** (`POST /materials/upload`): лимит на компанию → размер →
`detect_format` (сигнатура, zip-бомба) → `extract_isolated` в дочернем
процессе (транзакция закрыта) → дубликат по sha256 в пределах компании →
материал `PENDING` + `IngestJob` + аудит одной транзакцией. Воркер:
`claim_next` (`SKIP LOCKED`, застрявшие через 15 мин) → `preprocess` →
`split_document` → эмбеддинги документов (слоты квоты) → чанки заменяются
→ `READY`; ошибка — повтор 30 с × 2ⁿ до 5 раз, затем `FAILED` с кодом.

**Коннектор** (`/connectors`, `services/connector_service.py`): админ
создаёт подключение по спецификации вида (форма проверяется, адрес —
через `validate_outbound_url`), задаёт учётные данные (шифруются
`SecretBox`, ставится синхронизация) или, в режиме `per_user`, каждый
сотрудник авторизует себя сам: вводом полей (`PUT
/connectors/{id}/mine`) или OAuth — `POST /connectors/{id}/oauth/start`
отдаёт адрес портала с подписанным `state`, портал возвращает браузер
на `GET /connectors/oauth/callback` (без нашего токена: кто и куда —
из `state`), код меняется на пару токенов, токен проверяется
`adapter.check`, грант записывается, ставится синхронизация. Секрет
приложения (`client_secret`) — в `connectors.credentials`, задаёт
админ тем же `PUT .../credentials`; при работе с источником он
складывается с токенами сотрудника. Адаптер, продливший токен, отдаёт
новую пару через `refreshed_credentials`, ядро перешифровывает её в
грант при любом исходе запуска. Ошибка уровня подключения
(`AdapterConfigError`: секрет отвергнут, нет scope) останавливает
коннектор, не трогая гранты. Адаптеры: `connectors/bitrix24/` — REST
через `OutboundClient` (2 запроса/с, `next`-страницы, коды ошибок,
редирект = «портал переехал», скачивание только с хоста портала),
модули `disk` (общий диск и группы), `disk_personal`, `knowledge_base`
(сайты KNOWLEDGE/GROUP → страницы → HTML блоков), `knowledge_base_v2`
(REST 3.0 `note.*` → Markdown как есть); `connectors/confluence/` —
режим `organization`: пространства → страницы с предками →
ограничения чтения по цепочке с раскрытием групп → `visibility` и
`allowed_emails` по шаблону почты, вложения, storage-формат → HTML;
`connectors/yandex/` — режим `per_user` с OAuth Яндекс ID, обход Диска
сотрудника (общие папки внутри), скачивание по подписанной ссылке.
`cli connector-check`
гоняет адаптер против источника без базы и умеет записывать ответы в
фикстуры. Планировщик
воркера раз в минуту ставит в `connector_sync_jobs` подключения с
истёкшим интервалом. `ConnectorSyncService.run` в `tenant_scope`:
расшифровка → `check` → `list` → по документу: сравнение версии →
`fetch` → `detect_format` + песочница (файл) или `html_to_markdown`
(страница) → материал (`connector_id`, `external_id`, `source_url`,
`visibility`) + `IngestJob` + `material_access` — одной транзакцией на
документ; после полного листинга исчезнувшие удаляются. Права: режим
`organization` — из `allowed_emails` адаптера по `users.email`; режим
`per_user` — документ виден тому, в чьём листинге он есть. Поиск чанков
фильтрует по спрашивающему (`_visible_to`). Отвергнутые учётные данные
останавливают коннектор (`status=error`) или грант (`expired`) до
вмешательства человека; недоступный источник — повтор задачи до 3 раз.

**Вход**: лимиты по IP и по паре компания+почта → `verify_password` с
пустышкой → пара токенов; refresh: поиск по хешу `FOR UPDATE`, повторное
использование отзывает семейство и пишет аудит.

**Ночью** (`cli purge`, `cli gaps --all`): удаление `qa_log` по сроку,
журнала запусков коннекторов старше 90 дней и аудита старше года; по каждой активной компании в её `tenant_scope` —
классы, кластеры, сопоставление, подпись моделью, запись.

---

## 6. Данные и миграции

Alembic, `alembic upgrade head`; в CI — на пустой базе под владельцем
схемы **без суперпользователя**, с `alembic check` и полным даунгрейдом.
`MIGRATIONS_DATABASE_URL` отделяет роль миграций от роли приложения.

Тенантские таблицы (все под RLS): `users`, `materials`, `chunks`,
`qa_log`, `glossary_terms`, `gap_clusters`, `gap_cluster_questions`,
`connectors`, `connector_user_grants`, `connector_sync_runs`,
`material_access`. Очереди без RLS: `ingest_jobs`, `connector_sync_jobs`.

Ловушки, закреплённые в коде: `postgresql.ENUM(...).create(checkfirst=True)`
для новых enum (и `create_type=False` при переиспользовании существующего); FORCE RLS и массовые правки; генерируемая колонка `fts`;
переиндексация из миграции при смене размерности векторов.

---

## 7. Тесты

790+ тестов, `pytest-asyncio` в режиме `auto`. Схема пересоздаётся на
прогон (`create_all` + `apply_all`), тесты работают под ролью
`corp_ed_app_test` без `SUPERUSER`/`BYPASSRLS` — иначе тесты RLS ничего не
доказывали бы. Внешние сервисы — фейки; Redis-реализации проверяются с
настоящим Redis (`TEST_REDIS_URL`, в CI обязателен).

| Каталог | Что |
|---|---|
| `tests/security/` | токены, пароли, периметр, лимиты, аудит, RLS |
| `tests/api/` | каждая группа ручек: роли, изоляция, валидация, коды |
| `tests/llm/`, `tests/ingest/` | адаптеры и разбор ответов, файлы и песочница |
| `tests/test_*.py` | сервисы: FAQ, гибрид, кредиты, компании, ингест, воркер, отчёт о пробелах, изоляция |
| `tests/live/` | живая синхронизация с тестовым порталом Битрикс24; пропуск без `BITRIX24_TEST_*` |
| `tests/ml_eval/`, `test_split*`, `test_gaps`, … | тесты ML (не редактируются бэкендом) |

Порог покрытия в CI — 85 % (текущее ≈ 89 %). mypy strict — на `src`.

---

## 8. Стек

**Приложение:** Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg,
PostgreSQL 16 + pgvector, Alembic, Redis 7, Pydantic v2 +
pydantic-settings, PyJWT, argon2-cffi, structlog, httpx, uvicorn; разбор
файлов — mammoth + markdownify, pymupdf4llm (AGPL, принято); ML —
numpy, razdel.

**Внешние сервисы:** Yandex Cloud — Alice AI LLM Flash (OpenAI-совместимый
API), `text-embeddings-v2` (768). Выбор — 152-ФЗ и замеры ML.

**Разработка и CI:** uv, ruff (в том числе правила bandit), mypy strict,
pre-commit, pytest + coverage, Docker Compose; GitHub Actions — тесты,
миграции, образ; pip-audit, gitleaks, CodeQL, Trivy, SBOM; Dependabot.

---

## 9. Чего в системе нет

- Фронтенда (демо — krontoai.ru, отдельный репозиторий).
- Ручек для оплаты: пул кредитов считается, счета не выставляются.
- MFA, восстановления пароля по почте (пароль сбрасывает администратор).
- Small-to-big (BH-13) — контракт ML чистовой, решение ML — после MVP (по судье на золотом dev прирост в пределах шума).
- Планировщика внутри compose — cron на хосте.
- Собственного шифрования полей — уровень диска и бэкапов.
