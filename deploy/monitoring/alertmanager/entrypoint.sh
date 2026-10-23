#!/bin/sh
# Собирает конфиг Alertmanager из переменных и запускает его. Каналы
# (можно оба, получатель один — team):
# - ALERT_EMAIL_URL — webhook сервиса alert-mail (compose.yaml), письмо на
#   TEAM_NOTIFY_EMAIL через Postbox. Основной: с сервера в России Telegram
#   может быть недоступен (RISKS №50);
# - TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (TEAM_NOTIFY_TELEGRAM_* из .env
#   приложения). Токен — в файле с правами 600, не в конфиге и не в
#   аргументах процесса.
# Нет ни одного — тревоги видны только в Alertmanager и Grafana.
set -eu

config=/tmp/alertmanager.yml
channels=""
if [ -n "${ALERT_EMAIL_URL:-}" ]; then
    case "$ALERT_EMAIL_URL" in
        http://*) ;;
        *) echo "ALERT_EMAIL_URL — http://сервис:порт/путь" >&2; exit 64 ;;
    esac
    channels="$channels
    webhook_configs:
      - url: $ALERT_EMAIL_URL
        send_resolved: true
        max_alerts: 20"
fi
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    case "$TELEGRAM_CHAT_ID" in
        ''|*[!0-9-]*) echo "TELEGRAM_CHAT_ID — число (id чата)" >&2; exit 64 ;;
    esac
    umask 077
    printf '%s' "$TELEGRAM_BOT_TOKEN" > /tmp/telegram_token
    channels="$channels
    telegram_configs:
      - bot_token_file: /tmp/telegram_token
        chat_id: $TELEGRAM_CHAT_ID
        parse_mode: HTML
        send_resolved: true
        message: '{{ template \"kronto.telegram\" . }}'"
fi
if [ -n "$channels" ]; then
    receiver="team"
    receivers="
  - name: team$channels"
else
    echo "Почта и Telegram не настроены: тревоги видны только в Alertmanager и Grafana" >&2
    receiver="none"
    receivers=""
fi

cat > "$config" <<CONFIG
templates:
  - /etc/alertmanager/telegram.tmpl
route:
  receiver: $receiver
  group_by: [alertname, job, queue, reason, kind, provider, suite]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    # Тревоги на наблюдении (observe="true", BH-49): видны в Alertmanager и
    # Grafana, но никуда не шлются, пока не ясны нормальные уровни.
    - matchers: ['observe="true"']
      receiver: none
    - matchers: ['severity="critical"']
      receiver: $receiver
      repeat_interval: 1h
inhibit_rules:
  # Стенд не отвечает целиком — частные тревоги про него не нужны.
  - source_matchers: ['alertname="NotReady"']
    target_matchers: ['alertname=~"ModelErrors|ErrorBudgetBurn.*|AnswersDegraded|Llm.*|Embeddings.*|YandexConfigError|IngestWaitingProvider|Decision.*|MlGeneral.*|MlDistance.*"']
receivers:
  - name: none$receivers
CONFIG

exec /bin/alertmanager --config.file="$config" --storage.path=/alertmanager
