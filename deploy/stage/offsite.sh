#!/usr/bin/env bash
# Копия ночного дампа вне сервера (DEPLOY.md §8). Дамп шифруется age
# открытыми ключами держателей (их двое, у каждого свой закрытый ключ —
# на сервер закрытые ключи не попадают), уходит в объектное хранилище
# (S3) через rclone, и копия скачивается обратно: хеш шифротекста должен
# совпасть с отправленным. Расшифровку проверяет держатель своим ключом
# (STAGE.md §4.11, раз в квартал).
#
# Вместе с дампом уходит зашифрованная копия .env приложения: в нём ключи
# шифрования подключений (CONNECTOR_SECRETS_KEYS), без них восстановленная
# база не прочтёт токены систем клиентов (RISKS №51). Копия .env
# отправляется, только когда он изменился: имя — по хешу содержимого.
#
#   offsite.sh upload <дамп>  зашифровать, отправить, сверить. Ночью —
#                             backup.sh после restore-check.sh: неудача —
#                             неудача задачи backup, мониторинг тревожит.
#   offsite.sh report         строка для проверки после выкатки
#                             (check.sh). Свежий дамп ещё не отправлен —
#                             отправляет сейчас.
#
# Настройки — /etc/kronto/offsite.env (root:deploy, 640; образец —
# offsite.env.example). Нет файла — копия не настроена: upload ничего не
# делает, report пишет «--». На стенде данные тестовые, копия по желанию;
# на бою она обязательна.
#
# В строке отчёта — имя, размер и сверка хеша: адрес хранилища, бакет и
# ошибки rclone — в /var/log/kronto/offsite.log на сервере.
set -euo pipefail
# Зашифрованная копия, лог и результат — только для владельца.
umask 077

CONF="${KRONTO_OFFSITE_ENV:-/etc/kronto/offsite.env}"
BACKUPS="${KRONTO_BACKUPS:-/var/backups/kronto}"
APP_ENV="${KRONTO_APP_ENV:-/opt/kronto/.env}"
RESULT="${KRONTO_OFFSITE_RESULT:-/var/lib/kronto/offsite}"
LOG="${KRONTO_OFFSITE_LOG:-/var/log/kronto/offsite.log}"

latest() {
    find "$BACKUPS" -maxdepth 1 -name 'kronto-*.dump' -printf '%T@ %p\n' \
        | sort -rn | head -n 1 | cut -d' ' -f2-
}
# Дамп узнаётся по имени и времени: второй дамп за день — тот же файл.
dump_key() { echo "$(basename "$1")@$(stat -c %Y "$1")"; }

load() {
    # Переменные rclone (RCLONE_CONFIG_OFFSITE_*) нужны ему в окружении.
    set -a
    # shellcheck source=/dev/null
    . "$CONF"
    set +a
    # Файла конфига у rclone нет — remote описан переменными.
    export RCLONE_CONFIG=/dev/null
    : "${OFFSITE_AGE_RECIPIENT:?в $CONF нет OFFSITE_AGE_RECIPIENT}"
    # Несколько держателей — ключи через пробел: расшифрует любой из них.
    recipients=()
    local key
    for key in $OFFSITE_AGE_RECIPIENT; do
        recipients+=(-r "$key")
    done
    : "${OFFSITE_REMOTE:?в $CONF нет OFFSITE_REMOTE}"
    local tool
    for tool in age rclone; do
        command -v "$tool" >/dev/null || {
            echo "нет $tool: sudo apt install age rclone" >&2
            return 1
        }
    done
}

# Утренний upload и проверка после выкатки не должны слать одно и то же
# одновременно.
lock() {
    exec 9>"$RESULT.lock"
    flock -w 900 9
}

# Копия .env: имя по хешу содержимого, отправляется, только если такой
# в хранилище ещё нет. Срок хранения у неё тот же, что у дампов (правило
# бакета), поэтому раз в неделю копия обновляется и без изменений.
upload_env() {
    local work="$1" hash name enc
    [[ -r "$APP_ENV" ]] || { echo "нет $APP_ENV" >>"$LOG"; return 1; }
    hash=$(sha256sum "$APP_ENV" | cut -c1-16)
    name="env-$(date +%G-W%V)-$hash.age"
    if rclone lsf --s3-no-check-bucket "$OFFSITE_REMOTE/$name" 2>>"$LOG" | grep -q .; then
        return 0
    fi
    enc="$work/$name"
    age "${recipients[@]}" -o "$enc" "$APP_ENV" 2>>"$LOG" &&
        rclone copyto --s3-no-check-bucket "$enc" "$OFFSITE_REMOTE/$name" >>"$LOG" 2>&1 &&
        echo "$(date -u +%FT%TZ) $name" >>"$LOG"
}

upload() {
    local dump="$1"
    [[ -f "$dump" ]] || { echo "нет дампа $dump" >&2; return 1; }
    [[ -r "$CONF" ]] || { echo "offsite: не настроена ($CONF)"; return 0; }
    # report вызывает upload внутри «|| true» — там set -e не действует,
    # поэтому шаги до отправки проверяются явно.
    load || return 1
    lock || return 1
    local name state=ok detail started=$SECONDS work enc sent got size
    name="$(basename "$dump").age"
    work=$(mktemp -d "$BACKUPS/.offsite.XXXXXX") || return 1
    # shellcheck disable=SC2064 # путь известен сейчас
    trap "rm -rf '$work'" EXIT
    enc="$work/$name"
    echo "$(date -u +%FT%TZ) $name" >>"$LOG"
    if ! age "${recipients[@]}" -o "$enc" "$dump" 2>>"$LOG"; then
        state=fail detail="не зашифрован: age с ошибкой ($LOG на сервере)"
    elif ! rclone copyto --s3-no-check-bucket "$enc" "$OFFSITE_REMOTE/$name" \
        >>"$LOG" 2>&1; then
        state=fail detail="не отправлен: rclone с ошибкой ($LOG на сервере)"
    else
        sent=$(sha256sum "$enc" | cut -d' ' -f1)
        size=$(du -h "$enc" | cut -f1)
        # Не ETag: при составной загрузке это не MD5 файла. Копия
        # скачивается целиком — так видно, что в хранилище она цела.
        if ! got=$(rclone cat "$OFFSITE_REMOTE/$name" 2>>"$LOG" | sha256sum | cut -d' ' -f1); then
            state=fail detail="$name ($size) отправлен, но не скачан обратно ($LOG на сервере)"
        elif [[ "$got" == "$sent" ]]; then
            detail="$name ($size) за $((SECONDS - started)) с, скачан обратно — хеш совпал"
        else
            state=fail detail="$name ($size): скачанная копия не совпала с отправленной"
        fi
    fi
    if [[ "$state" == ok ]] && ! upload_env "$work"; then
        state=fail detail+="; копия .env не отправлена ($LOG на сервере)"
    fi
    if [[ "$state" == ok && -n "${OFFSITE_KEEP_DAYS:-}" ]]; then
        # Хранилище без правила срока хранения: старое удаляет сервер (ключу
        # тогда нужно и удаление). Ошибка удаления — не повод терять отчёт.
        rclone delete --min-age "${OFFSITE_KEEP_DAYS}d" --include 'kronto-*.dump.age' \
            "$OFFSITE_REMOTE" >>"$LOG" 2>&1 || detail+="; старые копии не удалены ($LOG)"
    fi
    rm -rf "$work"
    trap - EXIT
    printf '%s\t%s\t%s\n' "$(dump_key "$dump")" "$state" "$detail" >"$RESULT.tmp"
    mv "$RESULT.tmp" "$RESULT"
    echo "$(date -u +%FT%TZ) offsite $name $state"
    [[ "$state" == ok ]]
}

report() {
    if [[ ! -r "$CONF" ]]; then
        echo "--  копия вне сервера — не настроена (STAGE.md §4.11)"
        return 0
    fi
    local file checked="" state="" detail=""
    file=$(latest)
    if [[ -z "$file" ]]; then
        echo "FAIL копия вне сервера — дампов в $BACKUPS нет"
        return 1
    fi
    [[ -f "$RESULT" ]] && IFS=$'\t' read -r checked state detail <"$RESULT"
    if [[ "$checked" != "$(dump_key "$file")" ]]; then
        upload "$file" >/dev/null 2>>"$LOG" || true
        checked="" state="" detail=""
        [[ -f "$RESULT" ]] && IFS=$'\t' read -r checked state detail <"$RESULT"
        if [[ "$checked" != "$(dump_key "$file")" ]]; then
            echo "FAIL копия вне сервера — $(basename "$file") не отправлен ($LOG на сервере)"
            return 1
        fi
    fi
    if [[ "$state" == ok ]]; then
        echo "OK  копия вне сервера — $detail"
    else
        echo "FAIL копия вне сервера — $detail"
        return 1
    fi
}

case "${1:-}" in
upload) upload "${2:?offsite.sh upload <дамп>}" ;;
report) report ;;
*)
    echo "usage: offsite.sh upload <дамп> | report" >&2
    exit 64
    ;;
esac
