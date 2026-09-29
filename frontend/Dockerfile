# ===== Стадия 1: сборка статики =====
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build

WORKDIR /app
# Зависимости — отдельным слоем: меняются реже кода.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ===== Стадия 2: nginx без root, только статика =====
# API сюда не проксируется: /api и /health внешний прокси ведёт прямо на
# api:8000 (docs/DEPLOY.md §5) — цепочка X-Forwarded-For к API не меняется.
FROM nginxinc/nginx-unprivileged:1.30-alpine@sha256:ed04ec1ff34502c339ee5c3ae3f855442398edc1d05591e2b98981dcbbd20b1e AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Базовый образ и так работает под uid 101 (nginx); явная строка — для
# сканера (Trivy DS-0002) и против смены пользователя в базовом образе.
USER 101

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
