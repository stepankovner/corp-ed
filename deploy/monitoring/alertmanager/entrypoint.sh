#!/bin/sh
# Собирает конфиг Alertmanager из переменных и запускает его.
# Есть TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (TEAM_NOTIFY_TELEGRAM_* из
# .env приложения) — тревоги уходят в Telegram; нет — только в интерфейс.
# Токен — в файле с правами 600, не в конфиге и не в аргументах процесса.
set -eu

config=/tmp/alertmanager.yml
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    case "$TELEGRAM_CHAT_ID" in
        ''|*[!0-9-]*) echo "TELEGRAM_CHAT_ID — число (id чата)" >&2; exit 64 ;;
    esac
    umask 077
    printf '%s' "$TELEGRAM_BOT_TOKEN" > /tmp/telegram_token
    receiver="telegram"
    receivers="
  - name: telegram
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: $TELEGRAM_CHAT_ID
        parse_mode: HTML
        send_resolved: true
        message: '{{ template \"kronto.telegram\" . }}'"
else
    echo "Telegram не настроен: тревоги видны только в Alertmanager и Grafana" >&2
    receiver="none"
    receivers=""
fi

cat > "$config" <<CONFIG
templates:
  - /etc/alertmanager/telegram.tmpl
route:
  receiver: $receiver
  group_by: [alertname, job, queue, reason]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['severity="critical"']
      receiver: $receiver
      repeat_interval: 1h
inhibit_rules:
  # Стенд не отвечает целиком — частные тревоги про него не нужны.
  - source_matchers: ['alertname="NotReady"']
    target_matchers: ['alertname=~"ModelErrors|ErrorBudgetBurn.*|AnswersDegraded"']
receivers:
  - name: none$receivers
CONFIG

exec /bin/alertmanager --config.file="$config" --storage.path=/alertmanager
