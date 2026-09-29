#!/usr/bin/env bash
# Сквозная проверка стенда (python -m corp_ed.stand check) с самого
# сервера — после каждой выкатки ее вызывает workflow Deploy
# (corp-ed-deploy check). Вход, загрузка документа, индексация воркером,
# ответ по документу с настоящей моделью, вопрос вне документов, кредиты.
#
# Для проверки нужна компания с администратором. Её скрипт заводит сам
# при первом запуске, а пароль хранит только на сервере
# (/var/lib/corp-ed/stand-check.env, 600) — ни в GitHub, ни в чат он не
# попадает. Вручную: sudo -u deploy SSH_ORIGINAL_COMMAND=check /usr/local/bin/corp-ed-deploy
set -euo pipefail
# shellcheck source=/dev/null
. /etc/corp-ed/stage.env

COMPANY=stand-check
EMAIL=stand-check@krontoai.ru
state=/var/lib/corp-ed/stand-check.env
cd "$APP_DIR"

if [[ ! -f "$state" ]]; then
    # Сначала состояние, потом компания: если между ними что-то упадёт,
    # пароль не потеряется.
    temp="Tmp-$(openssl rand -hex 16)"
    ( umask 077; printf 'PASSWORD=Chk-%s\nTEMP=%s\n' "$(openssl rand -hex 16)" "$temp" > "$state" )
    printf '%s\n' "$temp" | docker compose -f compose.yaml run --rm --no-deps -T api \
        python -m corp_ed.cli create-tenant --code "$COMPANY" --name "Проверка стенда" \
        --seats 5 --admin-email "$EMAIL" --admin-password-stdin --not-found-mode general >/dev/null
    echo "==> заведена компания $COMPANY для проверки"
fi
# shellcheck source=/dev/null
. "$state"

# Модуль stand не читает настроек и не ходит в базу — только HTTP API,
# тем же путём, что браузер: https://DOMAIN через nginx. Имя указывает на
# шлюз Docker, то есть на этот же хост: публичный адрес изнутри облака
# доступен не везде.
run_check() {
    CORP_ED_PASSWORD="$1" CORP_ED_NEW_PASSWORD="${2:-}" \
    CORP_ED_BASE_URL="https://$DOMAIN" CORP_ED_COMPANY="$COMPANY" CORP_ED_EMAIL="$EMAIL" \
        docker run --rm --add-host "$DOMAIN:host-gateway" \
        -e CORP_ED_BASE_URL -e CORP_ED_COMPANY -e CORP_ED_EMAIL -e CORP_ED_PASSWORD -e CORP_ED_NEW_PASSWORD \
        corp-ed:local python -m corp_ed.stand check
}

logged_in() { grep -q '^OK  вход администратора' <<<"$1"; }

status=0
if [[ -n "${TEMP:-}" ]]; then
    # Первый вход: временный пароль сменится на постоянный.
    out=$(run_check "$TEMP" "$PASSWORD" 2>&1) || status=$?
    if ! logged_in "$out"; then
        # Смена могла пройти, а ответ — потеряться: пробуем постоянный.
        status=0
        out=$(run_check "$PASSWORD" 2>&1) || status=$?
    fi
    if logged_in "$out"; then
        ( umask 077; printf 'PASSWORD=%s\n' "$PASSWORD" > "$state" )
    fi
else
    out=$(run_check "$PASSWORD" 2>&1) || status=$?
fi
printf '%s\n' "$out"
exit "$status"
