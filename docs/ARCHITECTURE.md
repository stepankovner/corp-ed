# Архитектура corp-ed

Карта системы на 1 октября 2026. Описывает то, что существует в коде,
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
  синхронизация коннекторов), `cli.py` (команды команды Kronto), `stand.py` (проверка стенда через HTTP API: `python -m corp_ed.stand check`).

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
| `/invites` | `POST`, `GET`, `DELETE {id}`; `POST preview`, `POST accept` | создание и список — ADMIN; просмотр и принятие ссылки — без токена |
| `/leads` | `GET form`, `POST` | без токена (запись на созвон со страницы тарифов; приём выключен до политики ПДн) |
| `/connectors` | `GET kinds`, `GET tariff`, `GET`, `POST`, `GET/PATCH/DELETE {id}`, `PUT {id}/credentials`, `POST {id}/test`, `POST {id}/sync`, `GET {id}/runs`; сотрудник: `GET mine`, `GET/PUT/DELETE {id}/mine`, `POST {id}/oauth/start`; `GET oauth/callback` | управление — ADMIN; «Мои источники» — любой; обратный вызов OAuth — по подписанному `state` |
| `/health`, `/health/ready`, `/metrics`, `/` | `GET` | без токена; `/metrics` наружу не проксируется (`DEPLOY.md` §10) |

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
| `FaqService` | ответ: пул кредитов → история диалога (Redis) и переписывание уточняющего вопроса → словарь → эмбеддинг → поиск (вектор или гибрид) → реранкер (если задан `RAG_RERANK_MODEL`) → порог → промпт → пометка/отказ → `qa_log`; оценка; отладочный поиск |
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
| `Tenant` (не тенантская) | компания: код, активность, `seats`, `not_found_mode`, `tariff`, `connector_limit` |
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
| `RagSettings` | `RAG_*` | нарезка, лимит, порог, бюджет, температура, способ поиска — **без дефолтов**, значения за ML; память диалога и реранкер — с дефолтами (3 пары; реранкер выключен) |
| `GapsSettings` | `GAPS_*` | отчёт о пробелах: значения ML без дефолтов, инженерные потолки с дефолтами |
| `BillingSettings` | `BILLING_*` | кредиты: предложение досье |
| `HttpSettings` | — | CORS, хосты, Redis, лимиты тела; в `production` — обязательные проверки |
| `IngestSettings` | `INGEST_*` | песочница разбора, модель разметки PDF, `.xlsx`/`.pptx`/`.doc` (`INGEST_EXTRA_FORMATS`) |
| `ConnectorSettings` | `CONNECTOR_*` | ключи шифрования секретов, лимиты и бюджет синхронизации, OAuth, egress-прокси |
| `LeadSettings` | `LEADS_*` | приём заявок на созвон и версия политики ПДн |
| `TeamNotifySettings` | `TEAM_NOTIFY_*` | бот команды в Telegram |

`EMBEDDING_DIM = 768` — константа схемы: настройка обязана ей равняться.

### 3.2. База данных и изоляция

`get_engine`/`get_session_maker` — один движок на процесс. Три хука
(`core/database.py`): `after_begin` ставит GUC `app.tenant_id`
(`set_config(..., true)` — на транзакцию), `do_orm_execute` подмешивает
фильтр тенанта в ORM-select и синхронизирует GUC, `before_flush`
проверяет `tenant_id` записываемых объектов. `tenant_context.py`:
`current_tenant`, `require_tenant()`, `tenant_scope()` с обязательным
сбросом; `current_account` и `account_scope()` — учётка из токена (ТЗ
§2, 03.10).

**Учётка и членство.** `accounts` — человек (почта, пароль, имя,
подтверждение почты), не тенантская и не под RLS: вход ищет учётку до
того, как известна компания. `users` — членство учётки в компании (роль,
статус `active/blocked/pending/left`), тенантская; на `users.id` по-прежнему
ссылаются журнал вопросов, доступы к документам, подключения. Свои
членства во всех компаниях учётка видит через политику RLS
`own_membership` (`FOR SELECT`, `account_id = app.account_id`) и
`execution_options(account_memberships=True)`, снимающий фильтр ORM;
писать в чужую компанию она не даёт. **Правило:** членство меняется
только внутри `tenant_scope` его компании и там же сбрасывается в базу
(`flush`) — вне контекста RLS не находит строку, и UPDATE молча ничего
не меняет (ошибка входа, найденная 03.10). API-тесты выполняют запрос
в чистом контексте (`CleanContextTransport`), как на сервере.

`db_policies.py` — SQL, которого нет в моделях: политики RLS с `FORCE`
для `TENANT_TABLES`, триггер неизменяемости аудита. Один источник для
миграций и для тестовой схемы. Здесь же задокументирована ловушка:
под FORCE владелец-несуперпользователь не видит строк в миграции —
массовые правки идут через `NO FORCE`/`FORCE` или `TRUNCATE`.

### 3.3. Безопасность

`security.py`: argon2id с пустышкой для одинакового времени, access JWT
с полным набором claims (`sub` — учётка, `ver` — её версия; при выбранной
компании `tenant_id`, `member_id`, `mver` — версия членства), непрозрачный
refresh и его хеш, коды приглашений (base32 Крокфорда) и писем, временный
пароль. `mail.py`: отправка писем — SMTP, лог, память. `password_policy.py`: правила пароля. `rate_limit.py`:
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
`httpx.AsyncClient`, задачи ответов чата и флаги «Остановить» (Redis).
При остановке процесса начатые ответы дописываются до 15 секунд.
Докс выключены в `production`.

Middleware, снаружи внутрь: `CORS` → `SecurityHeaders` → `RequestID` →
`Metrics` → `TrustedHost` → `BodySizeLimit`. Порядок важен: заголовки безопасности и
`request_id` есть и у 400 от `TrustedHost`, и у 413.

---

## 4. `llm` и `ingest`

- `types.py`, `gateway.py` (`LLMGateway.generate(messages, temperature,
  max_tokens, response_format)`; `stream(...)` — куски текста и последним
  `Completion`, по умолчанию один кусок через `generate`),
  `embedding_gateway.py`. `yandex_openai.py` отдаёт настоящий поток
  (`stream=true`, расход — `stream_options.include_usage`, без него —
  оценка), повторяет только до первого байта и, если провайдер не принял
  поток (400/404/422), зовёт модель без него.
- `errors.py`, `retry.py` — классификация и экспоненциальные повторы.
- `yandex.py` (нативный API), `yandex_openai.py` (OpenAI-совместимый,
  Alice AI LLM Flash) — выбираются `factory.build_llm_gateway` по
  `LLM_PROVIDER`; оба принимают семафор и `response_format` (строгий
  JSON). `yandex_embedding.py` — `text-embeddings-v2`, dim 768, отдельные
  ограничители для вопросов и документов. `throttle.py` — слоты квоты в
  Redis (Lua + Redis TIME) с честным разделением долей.
- `fake.py`, `fake_embedding.py` — для тестов.
- `ingest/extract.py` — сигнатуры, zip-бомба, docx (mammoth →
  markdownify), pdf (pymupdf4llm, страницы через `\f`), doc, xlsx и pptx
  (свои разборщики ML без сторонних библиотек, `INGEST_EXTRA_FORMATS`),
  txt и md — UTF-8 как есть; `sandbox.py` —
  дочерний `python -I`, чистое окружение, таймаут, бюджет CPU = таймаут ×
  ядер (разбор многопоточный), убийство по лимиту → `timeout`;
  `extract_worker.py` — rlimits; модель разметки PDF — `INGEST_PDF_LAYOUT`; `preprocess.py` (ML) — чистка Markdown.
- `connectors/base.py` — `SourceAdapter` (`check`, `list`, `fetch`),
  `RemoteDocument` (id, версия, ссылка, права словами источника),
  `FetchedFile | FetchedPage`, `AdapterError`/`AdapterAuthError`;
  `registry.py` — `KindSpec` (режим, модули, поля формы и учётных
  данных, поле адреса) и `AdapterRegistry` (фабрики адаптеров);
  `html.py` — очистка HTML страниц до Markdown. Сеть — только через
  `core/outbound.py::OutboundClient` (проверка адреса и закрепление IP; за
  egress-прокси — по имени, `CONNECTOR_OUTBOUND_VIA_PROXY`, `DEPLOY.md` §9a).
  Адаптеры: Битрикс24, Confluence Server/DC, Яндекс 360 (Диск и Вики).

---

## 5. Основные потоки

**Регистрация и вход** (`/auth`, `services/account_service.py`,
`auth_service.py`; ТЗ §2–3): регистрация — имя, фамилия, почта, пароль,
согласие → учётка без подтверждённой почты и письмо с кодом из 6 цифр и
ссылкой (ответ одинаковый, есть ли учётка) → код (5 попыток на письмо)
или ссылка → сессия без компании. Вход — почта и пароль, без кода
компании; после входа выбрана последняя компания учётки, если членство в
ней действует. `switch-company` выдаёт новую пару токенов с другой
компанией. Блокировка или удаление из компании отзывают токены этой
компании (версия членства); обновление токена даёт сессию без неё, а не
выход. Восстановление пароля, смена почты (пароль и второй фактор,
подтверждение на новый адрес, «это не я» на старый), удаление учётки и
выход из компании — там же.
Письма кладутся в `outbox_emails` в транзакции действия, отправляет
`MailWorker` воркера (повторы с растущей паузой, текст стирается после
отправки).

**Вступление в компанию** (`/invites`): админ создаёт приглашение —
ссылку (`/join#токен`) и код `XXXX-XXXX` (оба хешами; компания по хешу —
через `invite_lookups`, не под RLS). Человек со своей учёткой вступает:
активное членство (место по тарифу) или «ждёт одобрения», если так
задано у приглашения. Новая компания — заявка `company_requests`,
одобряет команда (`cli requests approve`): компания создаётся,
заявитель — администратор.

**Профиль и справочник** (`/account`, `/people`, `/departments`,
`/avatars`; ТЗ §4, §7): личное — имя, отчество, телефон, Telegram, фото
— в учётке и общее для всех компаний человека; должность и отдел — в
членстве, свои в каждой компании. Фото перекодируется в WebP 256×256
(`services/avatar_service.py`) и лежит отдельно от учётки
(`account_avatars`); наружу — только подписанной ссылкой со сроком до
конца следующих суток, которую получают сам человек (в `/auth/me`) и
коллеги (в `/people`). Справочник — работающие люди компании из токена
под RLS; отделы заводит администратор.

**Админка** (`/company`, `/analytics`, `/folders`, `/sources/mine`,
`/logos`; ТЗ §5, §7, этап 7): настройки компании, которые раньше меняла
команда через CLI, — название, логотип, режим «ответа нет», правило
второго фактора, «запомнить устройство», домены почты
(`services/company_service.py`; каждое изменение — в журнал со «было /
стало»). Тариф и места по-прежнему задаёт команда: администратор
оставляет заявку, она приходит в Telegram команды (`TeamNotifier`).
Аналитика (`services/analytics_service.py`) считается из `qa_log` по дням
пояса биллинга, обезличенно; частые вопросы — от трёх разных людей.
Папки (`folders`, `folder_departments`) группируют загруженные документы
(`materials.folder_id`); папка с `restricted` открыта только своим
отделам и администраторам — правило в `ChunkRepository.visible_to`,
одном для поиска, «Где ищет ассистент» и ссылок «поделиться».
`/sources/mine` (`services/sources_service.py`) — что видит сотрудник:
папки с числом документов и источники компании с его подключением.
Интерфейс: «Обзор» — главная админки; «Источники» — вкладки «Файлы»
(папки и документы) и «Подключения» (коннекторы, «N из M» подключили
свой аккаунт); «Тариф» (бывший «Лимит вопросов») и «Настройки компании».

**Наша панель** (`/staff`, ТЗ §9, этап 8): команда kronto в браузере
вместо команд на сервере — заявки на компании, компании (тариф, места,
срок пилота `tenants.pilot_until`, приостановка), расход на модель ответа
по дням, моделям и компаниям (из `qa_log`; оценка в ₽ по
`BILLING_LLM_RUB_PER_1K_TOKENS`), поиск человека для помощи со входом,
заявки на созвон. Изменения — через те же `TenantService` и
`CompanyRequestService`, что и CLI; учётка команды (`current_staff`)
попадает в журнал. Сводка по компаниям — `services/staff_service.py`: у
каждой компании своя сессия и `tenant_scope`. Доступ —
`get_staff_account` (`staff_members` + надёжный второй фактор); в
`/auth/me` — `staff`, по нему фронт показывает «Панель kronto».

**Уведомления, первые шаги, помощь** (ТЗ §8, этап 9):
`NotificationService.notify_admins` пишет колокольчик каждому работающему
администратору и ставит письмо в очередь (`OutboxEmail`) — по его
настройкам (`notification_settings`, строки нет — всё включено). Вызывают
его сами события: `CreditService.note_spend` (80 % и исчерпан),
`ConnectorSyncService._stop_connector`, `InviteService.accept` (заявка на
вступление). Недельную сводку шлёт воркер (`digest_loop` →
`DigestService.send_due`): понедельник с 9:00 по поясу биллинга, раз в
неделю на компанию по отметке `digest.sent` в журнале; вручную — `cli
digest`. Первые шаги — `/onboarding` (чек-лист по данным компании,
отметки в членстве). «Написать в поддержку» — `/support`, команде —
`/staff/support`; в Telegram — только номер и тема.

**Сайт и песочница** (ТЗ §1, этап 10): страницы сайта — React-маршруты
в `frontend/src/site`, при сборке они рисуются в готовый HTML
(`site/prerender.tsx` + `tools/prerender.mjs` → `dist/<адрес>/index.html`),
остальные адреса — оболочка `app.html`; nginx: `try_files $uri
$uri/index.html /app.html`. Гостю на `/`, `/help` и неизвестных адресах
`RequireAuth` показывает страницу из `handle.guest` маршрута вместо
входа. Песочница: `GET /demo`, `POST /demo/ask` (ответ целиком) и
`POST /demo/ask/stream` (потоком, как чат; сайт берёт его) без входа →
`DemoService` в `tenant_scope` компании песочницы (`DEMO_COMPANY_CODE`)
своей сессией → `FaqService.answer_turn` без истории (режим STRICT).
Поток идёт из задачи внутри запроса: посетитель закрыл страницу — задача
отменяется, модель дальше не зовётся (ответ нигде не хранится, в отличие
от чата).
Компанию и её документы (`src/corp_ed/demo/documents`) заводит и
обновляет `cli demo setup` — его запускают выкатка и e2e.

**Вопрос сотрудника** (`POST /faq/ask`):
токен → тенант в контекст → лимит частоты → `CreditService.ensure_available`
(402 до платных вызовов) → режим компании → история диалога из Redis
(`conversation_id`, до `RAG_HISTORY_TURNS` пар, 12 часов) и переписывание
уточняющего вопроса в самостоятельный (таймаут — исходный вопрос) →
`expand_query` словарём (только для поиска) → эмбеддинг вопроса (слот
квоты) → поиск: `vector` (top-K, порог на каждой выдержке) или `hybrid`
(вектор ∪ полнотекст по 50, RRF, порог по лучшему вектору) → реранкер,
если задан `RAG_RERANK_MODEL` (правило ML `domain.rerank`, сбой — порядок
поиска) → `select_context` по бюджету токенов →
`build_faq_messages` → модель (семафор) → `normalize_citations` → если
отказ или пусто: общий ответ без выдержек с `ensure_general_prefix`
(или `NOT_FOUND_ANSWER` в строгом режиме) → строка `qa_log` + пороги
кредитов в одной транзакции → ответ с `origin`, `sources`, `answer_id`,
`diagnostics` (ADMIN).

**Чат** (`/conversations`, `/attachments`, `/suggestions`; ТЗ §6):
диалог — дерево `chat_messages` (`parent_id`): правка вопроса и «Ответить
заново» — соседние ветки, `conversations.current_message_id` — лист
показанной. Ход (`POST /conversations`, `…/messages`, `…/regenerate`):
проверки и пул кредитов **до** потока (обычные 402/404/409/422) → в одной
транзакции вопрос и пустой ответ «пишется» → фоновая задача процесса
(`services/chat_generation.py`, своя сессия БД) → `FaqService.answer_turn`
с историей из ветки (вместо Redis), выдержками вложений и `AnswerSink`
→ события `start / stage / origin / delta / reset / done | error` в
очередь → ответ HTTP `text/event-stream` пересылает их (`X-Accel-Buffering:
no`, пинг раз в 15 с). Обрыв соединения ответ не останавливает:
допишется и сохранится, фронт опрашивает диалог. «Остановить» —
`POST …/stop`: флаг в Redis (общий для процессов API), задача проверяет его
перед каждым куском и сохраняет текст до остановки (токены — оценкой).
Ответ, который «пишется» дольше 10 минут (задачу убил перезапуск), при
чтении становится «прерван». Вложение: разбор в песочнице →
`split_document` → до 6 000 токенов уходит в промпт целиком, больше —
эмбеддинги фрагментов в доле ингеста и 8 ближайших к вопросу; в базу
компании не попадает. «Поделиться» — токен на снимок ветки, открывают
коллеги по компании. Подсказки — от администратора и частые вопросы
`qa_log` (не меньше трёх разных людей, без масок ПДн и 👎).

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
`connectors/yandex/` — режим `per_user` с OAuth Яндекс ID: личный Диск
сотрудника (общие папки внутри), общие диски организации и Вики,
скачивание по подписанной ссылке (живьём не проверены, RISKS №38, №43).
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
журнала запусков коннекторов старше 90 дней, аудита старше года и
вложений чата, не отправленных с вопросом за сутки; по каждой активной компании в её `tenant_scope` —
классы, кластеры, сопоставление, подпись моделью, запись.

---

## 6. Данные и миграции

Alembic, `alembic upgrade head`; в CI — на пустой базе под владельцем
схемы **без суперпользователя**, с `alembic check` и полным даунгрейдом.
`MIGRATIONS_DATABASE_URL` отделяет роль миграций от роли приложения.

Тенантские таблицы (все под RLS): `users`, `materials`, `chunks`,
`qa_log`, `glossary_terms`, `gap_clusters`, `gap_cluster_questions`,
`connectors`, `connector_user_grants`, `connector_sync_runs`,
`material_access`, `invites`, `departments`, `conversations`,
`chat_messages`, `chat_attachments`, `chat_attachment_chunks`,
`chat_suggestions`, `folders`, `folder_departments`. Очереди без RLS:
`ingest_jobs`, `connector_sync_jobs`; без RLS и `leads` — заявки на
созвон, клиента ещё нет, читает только команда из CLI; `accounts` и
`account_avatars` — учётка человека вне компаний; `tenant_logos` —
логотип, переключатель показывает логотипы всех компаний человека
(наружу — только подписанной ссылкой); `staff_members` — команда kronto
для нашей панели (заводит только CLI); `support_requests` — обращения в
поддержку от учётки (с компанией или без). Под RLS также
`notifications` и `notification_settings`.

Ловушки, закреплённые в коде: `postgresql.ENUM(...).create(checkfirst=True)`
для новых enum (и `create_type=False` при переиспользовании существующего); FORCE RLS и массовые правки; генерируемая колонка `fts`;
переиндексация из миграции при смене размерности векторов.

---

## 7. Тесты

1 511 тестов (на 01.10), `pytest-asyncio` в режиме `auto`. Схема пересоздаётся на
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
| `tests/live/` | живые проверки: портал Битрикс24 (вебхук), Confluence в Docker (7.19, 8.5, 10.2 — `tests/live/confluence_dc/`), сквозной сценарий стенда с Yandex Cloud; без переменных окружения — пропуск |
| `tests/ml_eval/`, `test_split*`, `test_gaps`, … | тесты ML (не редактируются бэкендом) |

Порог покрытия в CI — 85 % (на 01.10 — 91,36 %). mypy strict — на `src`.

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
миграции, образы, фронтенд и сквозные тесты в браузере, выкатка на стенд
и его автовозобновление; pip-audit, gitleaks, CodeQL (с 30.09 падает:
репозиторий приватный, решение владельца ждёт), Trivy, SBOM; Dependabot.

---

## 9. Чего в системе нет

- Ручек для оплаты: пул кредитов считается, счета не выставляются.
- MFA, восстановления пароля по почте (пароль сбрасывает администратор).
- Small-to-big (BH-13) — контракт ML чистовой, решение ML — после MVP (по судье на золотом dev прирост в пределах шума).
- Планировщика внутри compose — cron на хосте.
- Собственного шифрования полей — уровень диска и бэкапов.
