# Образ агента сетевых папок kronto. Запуск — README.md рядом.
#
# Агент ничего не пишет на диск (состояние берёт из kronto через /state),
# поэтому контейнер работает с файловой системой только для чтения
# (`--read-only`) от непривилегированного пользователя.
#
# Сборка: docker build -t kronto-agent agent/
# За HTTPS-прокси со своим сертификатом — секрет build_ca:
#   docker build --secret id=build_ca,src=/путь/к/ca.pem -t kronto-agent agent/
FROM python:3.12.15-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=secret,id=build_ca,required=false,mode=0444 \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install . \
    && rm -rf /app/src /root/.cache \
    && groupadd --system --gid 10001 kronto \
    && useradd --system --uid 10001 --gid kronto --home-dir /nonexistent \
        --shell /usr/sbin/nologin kronto

USER 10001:10001
ENTRYPOINT ["kronto-agent"]
CMD ["run", "--config", "/etc/kronto-agent/agent.toml"]

# Для живого стенда (tests/live/smb, --target test): тот же образ плюс
# pytest; тесты монтируются в /app/tests.
FROM base AS test
USER root
RUN --mount=type=secret,id=build_ca,required=false,mode=0444 \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install "pytest>=9.1.1"
USER 10001:10001
WORKDIR /app
ENTRYPOINT []

# Образ по умолчанию (последняя стадия) — агент без pytest.
FROM base AS runtime
