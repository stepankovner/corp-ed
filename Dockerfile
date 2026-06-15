# ===== Стадия 1: builder — сборка зависимостей =====
FROM python:3.12-slim AS builder

# Копируем uv из официального образа uv (быстрее чем ставить через pip)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Рабочая директория внутри контейнера
WORKDIR /app

# Переменные окружения для uv:
# - UV_COMPILE_BYTECODE: компилировать .pyc для скорости старта
# - UV_LINK_MODE=copy: копировать пакеты, не симлинки (надёжнее в Docker)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Сначала копируем ТОЛЬКО файлы зависимостей (меняются редко -> кешируется)
COPY pyproject.toml uv.lock README.md ./

# Устанавливаем зависимости (без самого проекта пока, без dev-группы)
RUN uv sync --frozen --no-install-project --no-dev

# Теперь копируем код проекта (меняется часто -> отдельный слой)
COPY src/ ./src/

# Устанавливаем сам проект
RUN uv sync --frozen --no-dev

# ===== Стадия 2: runtime — финальный образ =====
FROM python:3.12-slim AS runtime

WORKDIR /app

# Копируем готовое виртуальное окружение и код из builder-стадии
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Добавляем venv в PATH, чтобы команды (uvicorn) были доступны напрямую
ENV PATH="/app/.venv/bin:$PATH"

# Порт который слушает приложение (документирующая директива)
EXPOSE 8000

# Команда запуска при старте контейнера
CMD ["uvicorn", "lms.main:app", "--host", "0.0.0.0", "--port", "8000"]
