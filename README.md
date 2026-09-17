# corp-ed

AI-конструктор адаптации стажёров для российского рынка. Две фичи:

- **генерация программы адаптации 30/60/90** по брифу руководителя;
- **FAQ-бот**, который отвечает строго по материалам компании (RAG)
  и честно отказывается, когда ответа в материалах нет.

Данные компаний изолированы друг от друга: `tenant_id` берётся
из подписанного токена, фильтр на чтении и проверка на записи навешаны
хуками SQLAlchemy.

Карта системы — в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
принятые решения — в [`docs/DECISIONS.md`](docs/DECISIONS.md),
известные слабые места — в [`docs/RISKS.md`](docs/RISKS.md).

## Стек

**Бэкенд:** Python 3.12, FastAPI, SQLAlchemy 2 (async, asyncpg),
PostgreSQL 16 + pgvector, Alembic, pydantic-settings, structlog.
Языковые модели — Yandex Cloud Foundation Models (требования 152-ФЗ
к размещению данных).

**Фронтенд:** Vite + React + TypeScript.

**Разработка:** uv, ruff, mypy (strict), pytest, pre-commit, Docker
Compose, GitHub Actions.

## Как поднять демо

Нужны [uv](https://docs.astral.sh/uv/), Docker и Node.js 20+.

### 1. Окружение

```bash
cp .env.example .env
```

Заполнить в `.env`:

- `SECRET_KEY` — `openssl rand -hex 32`
- `POSTGRES_PASSWORD` и пароль внутри `DATABASE_URL`
- `YC_FOLDER_ID` и `YC_API_KEY` — каталог и API-ключ сервисного аккаунта
  Yandex Cloud. Без них работают все ручки, кроме генерации программы
  и FAQ: они ходят в модель и вернут 502.

### 2. База и миграции

```bash
docker compose up -d db
uv sync
uv run alembic upgrade head
```

### 3. Демо-данные

```bash
uv run python scripts/seed_demo.py
```

Скрипт создаёт компанию с кодом `demo`, руководителя, стажёра, три
материала и один бриф, после чего печатает логины и пароли — они
генерируются при каждом запуске. Повторный запуск пересоздаёт
демо-компанию и не трогает остальные данные.

Материалы намеренно остаются непроиндексированными: индексация —
действие руководителя в интерфейсе.

### 4. Бэкенд

```bash
uv run uvicorn corp_ed.main:app --reload
```

API — http://localhost:8000, документация — http://localhost:8000/docs

### 5. Фронтенд

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

Интерфейс — http://localhost:5173. Адрес бэкенда задаётся
в `frontend/.env` (`VITE_API_URL`), список origin'ов, которым разрешён
доступ к API, — в корневом `.env` (`CORS_ALLOWED_ORIGINS`).

### Сквозной сценарий

1. Войти руководителем (код компании `demo`).
2. Добавить материал и нажать «Проиндексировать» — у материала появится
   число чанков.
3. Заполнить бриф и сгенерировать программу.
4. Войти стажёром и задать вопрос по материалам — придёт ответ
   с источниками.
5. Задать вопрос, ответа на который в материалах нет, — придёт отказ.

## Разработка

```bash
uv run ruff format          # форматирование
uv run ruff check --fix     # линтер
uv run mypy                 # типы (strict, только src)
uv run pytest -q            # тесты
uv run pre-commit install   # хуки, один раз
```

Тестам нужна отдельная база из `TEST_DATABASE_URL`: она создаётся один
раз (`createdb corp_ed_test`), таблицы в ней строятся из моделей, минуя
alembic.

## Структура

```
src/corp_ed/
  api/v1/         HTTP: эндпоинты, схемы, сборка зависимостей
  services/       бизнес-логика
  repositories/   доступ к данным
  domain/         модели и типы предметной области
  llm/            адаптеры к языковым моделям
  prompts/        промпты
  core/           конфиг, БД, безопасность, исключения, tenant-контекст
frontend/         интерфейс (Vite + React + TypeScript)
scripts/          засев демо-данных
migrations/       alembic
tests/            тесты
```

Направление зависимостей: `api → services → repositories → domain`.
Импорт вверх по цепочке — протечка слоя.
