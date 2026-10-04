#!/usr/bin/env bash
# Поднимается ли ночной дамп базы (DEPLOY.md §8). Дамп восстанавливается
# во временную базу того же сервера Postgres; там сверяются версия схемы,
# RLS и число строк; временная база удаляется.
#
#   restore-check.sh run [дамп]  проверить дамп (по умолчанию — свежий).
#                                Ночью — backup.sh сразу после дампа:
#                                дамп, который не поднимается, — неудача
#                                задачи backup, и мониторинг тревожит.
#   restore-check.sh report      строка для проверки после выкатки
#                                (check.sh). Свежий дамп ещё не проверен —
#                                проверяет сейчас; дампа за сутки нет
#                                (прерываемый сервер стоял ночью) — снимает.
#
# В строке отчёта — только счётчики: подробности ошибки pg_restore могут
# содержать данные из базы, они — в /var/log/kronto/restore-check.log.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/kronto/stage.env

BACKUPS=/var/backups/kronto
RESULT=/var/lib/kronto/restore-check
LOG=/var/log/kronto/restore-check.log
SCRATCH_DB=kronto_restore_check
# Дамп — раз в сутки (cron 04:00), с запасом на длинный дамп.
MAX_AGE_HOURS=26
SUMMARY="SELECT (SELECT version_num FROM alembic_version LIMIT 1),
    (SELECT count(*) FROM tenants), (SELECT count(*) FROM accounts),
    (SELECT count(*) FROM materials),
    (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL),
    (SELECT count(*) FROM pg_policies WHERE schemaname = 'public'),
    (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relrowsecurity AND c.relforcerowsecurity)"

cd "$APP_DIR"
db() { docker compose -f compose.yaml exec -T db "$@"; }
query() { db psql -U corp_ed -d "$1" -v ON_ERROR_STOP=1 -Atq -c "$2"; }
latest() {
    find "$BACKUPS" -maxdepth 1 -name 'kronto-*.dump' -printf '%T@ %p\n' \
        | sort -rn | head -n 1 | cut -d' ' -f2-
}
drop_scratch() { db dropdb -U corp_ed --if-exists "$SCRATCH_DB" >/dev/null 2>&1 || true; }
# Дамп узнаётся по имени и времени: второй дамп за день — тот же файл.
dump_key() { echo "$(basename "$1")@$(stat -c %Y "$1")"; }

# Ночной запуск и проверка после выкатки не должны делить временную базу.
exec 9>"$RESULT.lock"
flock -w 900 9

run() {
    local file="${1:-$(latest)}"
    [[ -n "$file" && -f "$file" ]] || { echo "дампов в $BACKUPS нет" >&2; return 1; }
    local name started detail state=ok
    name=$(basename "$file")
    drop_scratch # остаток прерванного запуска
    trap drop_scratch EXIT
    db createdb -U corp_ed "$SCRATCH_DB"
    started=$SECONDS
    echo "$(date -u +%FT%TZ) $name" >>"$LOG"
    if ! db pg_restore -U corp_ed -d "$SCRATCH_DB" --exit-on-error <"$file" \
        >/dev/null 2>>"$LOG"; then
        detail="не поднялся: pg_restore с ошибкой ($LOG на сервере)"
        state=fail
    else
        local version tenants accounts materials chunks policies forced
        local live_version live_policies live_forced
        IFS='|' read -r version tenants accounts materials chunks policies forced \
            <<<"$(query "$SCRATCH_DB" "$SUMMARY")"
        IFS='|' read -r live_version _ _ _ _ live_policies live_forced \
            <<<"$(query corp_ed "$SUMMARY")"
        detail="поднят за $((SECONDS - started)) с: схема ${version:-нет}, компаний $tenants,"
        detail+=" учёток $accounts, документов $materials, фрагментов с векторами $chunks"
        if [[ -z "$version" ]] || ((tenants < 1 || accounts < 1)); then
            detail+=" — пустая база"
            state=fail
        elif [[ "$version" != "$live_version" ]]; then
            # После дампа выкатка с миграцией: RLS сравнивать не с чем.
            detail+=", политик RLS $policies, таблиц с RLS $forced (живая база уже на $live_version)"
        elif [[ "$policies" == "$live_policies" && "$forced" == "$live_forced" ]]; then
            detail+=", политик RLS $policies, таблиц с RLS $forced — как в живой базе"
        else
            detail+=", RLS не как в живой базе: политик $policies из $live_policies, таблиц $forced из $live_forced"
            state=fail
        fi
    fi
    drop_scratch
    trap - EXIT
    # Отчёт после выкатки показывает сохранённый результат: сравнение с
    # живой базой — на момент этой проверки.
    detail+=" (проверка $(date -u '+%d.%m %H:%M') UTC)"
    printf '%s\t%s\t%s\n' "$(dump_key "$file")" "$state" "$detail" >"$RESULT.tmp"
    mv "$RESULT.tmp" "$RESULT"
    echo "$(date -u +%FT%TZ) restore-check $name $state"
    [[ "$state" == ok ]]
}

report() {
    local file name age when checked="" state="" detail=""
    file=$(latest)
    if [[ -n "$file" ]]; then
        age=$((($(date +%s) - $(stat -c %Y "$file")) / 3600))
        when="$age ч назад"
    fi
    if [[ -z "$file" ]] || ((age > MAX_AGE_HOURS)); then
        # Свежего дампа нет (прерываемый сервер стоял ночью): снять сейчас.
        # Остановленную задачу cron мониторинг ловит сам («мёртвая рука»,
        # 26 ч без успеха).
        when="снят сейчас: дампов не было"
        [[ -n "$file" ]] && when="снят сейчас: последний был $age ч назад"
        KRONTO_BACKUP_NO_VERIFY=1 "$APP_DIR/deploy/stage/backup.sh" >/dev/null || true
        file=$(latest)
    fi
    if [[ -z "$file" ]]; then
        echo "FAIL бэкап — дамп не снялся (backup.sh)"
        return 1
    fi
    name=$(basename "$file")
    [[ -f "$RESULT" ]] && IFS=$'\t' read -r checked state detail <"$RESULT"
    if [[ "$checked" != "$(dump_key "$file")" ]]; then
        run "$file" >/dev/null || true
        IFS=$'\t' read -r checked state detail <"$RESULT"
    fi
    if [[ "$state" == ok ]]; then
        echo "OK  бэкап — $name ($(du -h "$file" | cut -f1), $when) $detail"
    else
        echo "FAIL бэкап — $name ($when) $detail"
        return 1
    fi
}

case "${1:-}" in
run) run "${2:-}" ;;
report) report ;;
*)
    echo "usage: restore-check.sh run [дамп] | report" >&2
    exit 64
    ;;
esac
