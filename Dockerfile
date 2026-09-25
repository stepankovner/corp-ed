# ===== Стадия 1: builder — сборка зависимостей =====
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS builder

# uv — из официального образа, версия закреплена (та же, что у разработчиков
# и в CI): «latest» в базовом образе — это сборка, которая меняется сама.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

WORKDIR /app

# - UV_COMPILE_BYTECODE: компилировать .pyc для скорости старта
# - UV_LINK_MODE=copy: копировать пакеты, не симлинки (надёжнее в Docker)
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Сначала ТОЛЬКО файлы зависимостей (меняются редко -> кешируется)
COPY pyproject.toml uv.lock README.md ./

# Зависимости без самого проекта и без dev-группы. Alembic — в основных
# зависимостях: миграции накатываются этим же образом (RISKS №3).
RUN uv sync --frozen --no-install-project --no-dev

# Код проекта (меняется часто -> отдельный слой)
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# ===== Стадия 2: runtime — финальный образ =====
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

# Обновления безопасности базового образа: slim выходит по расписанию, а
# CVE в libc и openssl — нет. Списки пакетов не оставляем (размер).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь без домашней папки и без shell:
# уязвимость в парсере PDF или в зависимости не должна давать root в
# контейнере. Код и окружение принадлежат root и доступны только на чтение.
RUN groupadd --system app && useradd --system --gid app --no-create-home \
    --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
# Миграции — тем же образом: `alembic upgrade head` под ролью владельца
# схемы (MIGRATIONS_DATABASE_URL), см. compose.yaml и docs/DEPLOY.md.
COPY alembic.ini ./
COPY migrations/ ./migrations/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# Без базы и Redis намеренно: их сбой — не повод перезапускать приложение.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# --proxy-headers: за reverse proxy настоящий адрес клиента — из
#   X-Forwarded-For, но только от адресов из FORWARDED_ALLOW_IPS (переменная
#   окружения uvicorn; по умолчанию 127.0.0.1). Иначе лимиты частоты и
#   журнал аудита видели бы адрес прокси или подделанный заголовок.
# --no-server-header: не сообщать версию сервера.
CMD ["uvicorn", "corp_ed.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header"]
