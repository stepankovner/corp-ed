#!/usr/bin/env bash
# Ночная задача с отметкой для мониторинга («мёртвая рука», П-9):
#
#   cron-run.sh <имя> <команда…>
#
# Успех — время в corp_ed_cron_last_success_timestamp_seconds{job="<имя>"},
# неудача — в ..._last_failure_... (textfile-коллектор node-exporter,
# deploy/monitoring). Prometheus тревожит, если успеха нет больше 26 часов:
# так видно и упавшую задачу, и задачу, которая перестала запускаться.
set -uo pipefail

job="${1:?имя задачи}"
shift
[[ "$job" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "имя задачи: $job" >&2; exit 64; }
dir="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile}"

"$@"
status=$?

if [[ $status -eq 0 ]]; then
    kind=success
    help="Время последнего успешного запуска ночной задачи (unix time)."
else
    kind=failure
    help="Время последнего неудачного запуска ночной задачи (unix time)."
fi
if [[ -d "$dir" && -w "$dir" ]]; then
    # Атомарно: node-exporter не должен прочитать недописанный файл.
    tmp=$(mktemp "$dir/.corp_ed_${job}_${kind}.XXXXXX")
    {
        echo "# HELP corp_ed_cron_last_${kind}_timestamp_seconds $help"
        echo "# TYPE corp_ed_cron_last_${kind}_timestamp_seconds gauge"
        echo "corp_ed_cron_last_${kind}_timestamp_seconds{task=\"$job\"} $(date +%s)"
    } > "$tmp"
    chmod 644 "$tmp"
    mv "$tmp" "$dir/corp_ed_${job}_${kind}.prom"
fi
exit "$status"
