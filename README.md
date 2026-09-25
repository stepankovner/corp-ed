# corp-ed

Бэкенд ассистента по внутренним документам компании (Kronto).
Администратор компании загружает регламенты, положения и инструкции;
сотрудники задают вопросы и получают ответ со ссылками на выдержки;
администратор видит, каких документов в базе не хватает.

Несколько компаний на одной установке, изоляция данных — в трёх
независимых кольцах, включая Row-Level Security в PostgreSQL. Подробно —
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) и
[`docs/SECURITY.md`](docs/SECURITY.md).

## Стек

Python 3.12 · FastAPI · SQLAlchemy 2 (async) + asyncpg · PostgreSQL 16 +
pgvector · Redis · Alembic · Yandex Cloud (Alice AI LLM Flash,
text-embeddings-v2) · uv · ruff · mypy strict · pytest

## Запуск

### Docker Compose (разработка)

```bash
cp .env.example .env      # заполнить: пароли, YC_FOLDER_ID, YC_API_KEY, RAG_* от ML
docker compose up --build
```

Поднимаются PostgreSQL с pgvector, Redis, миграции, API с автоперезагрузкой
и воркер ингеста. API — http://localhost:8000, документация —
http://localhost:8000/docs (вне `production`).

Боевая форма (без `--reload`, без открытых портов базы, приложение под
ролью без DDL): `docker compose -f compose.yaml up -d` — см.
[`docs/DEPLOY.md`](docs/DEPLOY.md).

### Локально (uv)

Нужны PostgreSQL 16 с расширением `vector` и, по желанию, Redis.

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn corp_ed.main:app --reload
uv run python -m corp_ed.worker           # в отдельном терминале
```

### Первая компания

Компании заводит команда из CLI — HTTP-ручки для этого нет:

```bash
uv run python -m corp_ed.cli create-tenant \
    --code acme --name "ACME" --seats 50 --admin-email admin@acme.ru
```

Временный пароль администратора печатается один раз; при первом входе
система потребует его сменить. Остальные команды —
`python -m corp_ed.cli --help`.

## Проверки

```bash
uv run ruff check --fix && uv run ruff format
uv run mypy
TEST_DATABASE_URL=postgresql+asyncpg://corp_ed:…@localhost:5432/corp_ed_test \
TEST_REDIS_URL=redis://localhost:6379/15 \
uv run pytest -q --cov
uv run pre-commit install   # один раз
```

Тесты пересоздают схему тестовой базы и работают под ролью без
`SUPERUSER`/`BYPASSRLS`. Без `TEST_REDIS_URL` тесты Redis-реализаций
пропускаются.

## CI

`CI` — линтер (включая правила bandit), типы, тесты с порогом покрытия
85 %, миграции на пустой базе под владельцем без суперпользователя,
сборка образа и запуск не под root. `Security` (по push и еженедельно) —
pip-audit, gitleaks, CodeQL, Trivy по образу и конфигурации, SBOM.

## Структура

```
src/corp_ed/
  api/v1/         ручки, схемы, зависимости, лимиты частоты
  services/       бизнес-логика
  repositories/   доступ к данным
  domain/         модели и чистые функции (часть — ML)
  core/           конфиг, база и изоляция, безопасность, middleware
  llm/            адаптеры Yandex Cloud, ретраи, квоты
  ingest/         извлечение текста из файлов, песочница
  connectors/     адаптеры к системам-источникам (интерфейс, каталог, HTML)
  prompts/        промпты (ML)
  main.py         API · worker.py  ингест и синхронизация · cli.py  команды
migrations/       Alembic
deploy/postgres/  роль приложения
eval/             оценка качества (ML)
tests/            940+ тестов, в том числе tests/security/
docs/             ARCHITECTURE, DECISIONS, RISKS, SECURITY, DEPLOY, INTEGRATION, отчёты ML
```

## Документация

| Файл | Что там |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | карта системы: слои, потоки, данные |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | журнал решений: контекст, альтернативы, цена |
| [`docs/RISKS.md`](docs/RISKS.md) | что известно и не закрыто |
| [`docs/SECURITY.md`](docs/SECURITY.md) | модель угроз, меры с тестами, соответствие OWASP, чек-лист пентеста |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | роли базы, переменные, прокси, cron, бэкапы, ротация |
| [`docs/INTEGRATION.md`](docs/INTEGRATION.md) | статус контракта с ML (BH-1…24) |
| [`docs/CONNECTORS-RESEARCH.md`](docs/CONNECTORS-RESEARCH.md) | исследование перед коннекторами: статистика систем, как у других, что API отдают по правам |
| [`docs/WORKLOG.md`](docs/WORKLOG.md) | журнал работ: статистика тестов, что сделано, ошибки и исправления, план MVP |
| [`docs/backend-handoff.md`](docs/backend-handoff.md), `ml-*.md` | документы ML |

## Лицензии

Разбор PDF — `pymupdf`/`pymupdf4llm` под AGPL-3.0 (решение команды,
записано в `RISKS.md`). Остальные зависимости — MIT/BSD/Apache.
