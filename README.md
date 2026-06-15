# LMS — Learning Management System

Учебный проект для практики backend и MLOps. Платформа управления обучением:
курсы, студенты, преподаватели. Создаётся как полигон для освоения
production-grade практик разработки.

## Стек

- **Python 3.12**
- **FastAPI** — веб-фреймворк
- **uv** — управление зависимостями и окружением
- **Docker** — контейнеризация
- **ruff** — линтер и форматтер
- **mypy** — статическая проверка типов

## Требования

- [uv](https://docs.astral.sh/uv/) (установка: `brew install uv`)
- [Docker](https://www.docker.com/) (для запуска в контейнере)

## Запуск

### Локально (через uv)

```bash
# Установить зависимости
uv sync

# Запустить сервер разработки
uv run uvicorn lms.main:app --reload
```

Приложение будет доступно на http://localhost:8000
Документация API: http://localhost:8000/docs

### В Docker (через compose)

```bash
docker compose up --build
```

То же приложение на http://localhost:8000, с автоперезагрузкой при изменении кода.

## Разработка

```bash
# Линтинг и форматирование
uv run ruff check --fix
uv run ruff format

# Проверка типов
uv run mypy

# Установить pre-commit хуки (один раз)
uv run pre-commit install
```

## Структура проекта

```
src/lms/          # код приложения
  main.py         # точка входа FastAPI
tests/            # тесты
Dockerfile        # сборка production-образа
compose.yaml      # окружение для разработки
pyproject.toml    # зависимости и конфигурация инструментов
```
