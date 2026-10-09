#!/usr/bin/env bash
# Вторая линия для исходящих соединений контейнеров (DEPLOY.md §9a,
# RISKS №29). Первая — проверка адресов в коде (core/outbound.py); эта
# работает на уровне ядра и не зависит от кода приложения.
#
# Правила (своя таблица nftables, правила Docker не трогает):
# - из контейнеров наружу — только публичные адреса: частные сети,
#   loopback, link-local (там адрес метаданных облака 169.254.169.254),
#   CGNAT, multicast — запрещены; между контейнерами — как было;
# - DNS (порт 53) — только к резолверам самого хоста: внутренний DNS
#   Docker ходит к ним из сети контейнера;
# - к самому хосту из контейнеров — только 80 и 443 (nginx: внешняя
#   проверка мониторинга идёт на публичный адрес стенда).
#
# Под root:
#   deploy/stage/egress.sh install   — записать правила, включить при загрузке, проверить
#   deploy/stage/egress.sh check     — проверить из контейнера worker
#   deploy/stage/egress.sh remove    — снять правила (откат)
set -euo pipefail

TABLE="inet kronto_egress"
RULES=/etc/kronto/egress.nft
UNIT=/etc/systemd/system/kronto-egress.service
APP_DIR=${APP_DIR:-/opt/kronto}

die() { echo "egress: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запускать под root"
command -v nft >/dev/null || die "нет nft: apt-get install -y nftables"

# Резолверы хоста, к которым ходит внутренний DNS Docker. При
# systemd-resolved настоящие — в /run/systemd/resolve/resolv.conf
# (в /etc/resolv.conf там 127.0.0.53, и Docker его не использует).
resolvers() {
    local file=/etc/resolv.conf
    [[ -r /run/systemd/resolve/resolv.conf ]] && file=/run/systemd/resolve/resolv.conf
    awk '$1 == "nameserver" && $2 !~ /^127\./ && $2 !~ /:/ { print $2 }' "$file" | sort -u
}

write_rules() {
    local dns
    dns=$(resolvers | paste -sd, -)
    [[ -n "$dns" ]] || die "не нашёл резолверы хоста в resolv.conf"
    install -d -m 755 /etc/kronto
    umask 022
    cat > "$RULES" <<EOF
# Создан deploy/stage/egress.sh $(date -u +%FT%TZ). Не править руками:
# поменять egress.sh и запустить install заново.
table $TABLE
delete table $TABLE
table $TABLE {
    set blocked_v4 {
        type ipv4_addr; flags interval
        elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
                     169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24,
                     192.168.0.0/16, 198.18.0.0/15, 224.0.0.0/3 }
    }
    set blocked_v6 {
        type ipv6_addr; flags interval
        elements = { ::/127, 64:ff9b::/96, 64:ff9b:1::/48, fc00::/7, fe80::/10, ff00::/8 }
    }
    set host_dns {
        type ipv4_addr
        elements = { $dns }
    }

    # Раньше правил Docker (у них приоритет filter = 0).
    chain forward {
        type filter hook forward priority filter - 10; policy accept;
        ct state established,related accept
        # Между контейнерами — без изменений.
        oifname "docker0" accept
        oifname "br-*" accept
        iifname "docker0" jump from_container
        iifname "br-*" jump from_container
    }

    chain from_container {
        ip daddr @host_dns meta l4proto { tcp, udp } th dport 53 accept
        ip daddr @blocked_v4 counter drop
        ip6 daddr @blocked_v6 counter drop
    }

    chain input {
        type filter hook input priority filter - 10; policy accept;
        ct state established,related accept
        iifname "docker0" jump to_host
        iifname "br-*" jump to_host
    }

    chain to_host {
        tcp dport { 80, 443 } accept
        counter drop
    }
}
EOF
}

write_unit() {
    cat > "$UNIT" <<EOF
[Unit]
Description=kronto: outbound rules for containers (deploy/stage/egress.sh)
Wants=network-pre.target
Before=network-pre.target docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $RULES
ExecStop=/usr/sbin/nft delete table $TABLE

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable kronto-egress.service >/dev/null
}

# Пробы — из контейнера worker: у него тот же образ и та же сеть, что у
# api. Python в образе есть, curl — не обязательно.
probe() {
    local host=$1 port=$2
    (cd "$APP_DIR" && docker compose -f compose.yaml exec -T worker python - "$host" "$port") <<'PY'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4).close()
    print("open")
except OSError as exc:
    print("closed", type(exc).__name__)
PY
}

expect() {
    local want=$1 host=$2 port=$3 got
    got=$(probe "$host" "$port" | head -1)
    if [[ "$got" == "$want"* ]]; then
        echo "  ok    $host:$port — $got"
    else
        echo "  FAIL  $host:$port — $got (ожидалось: $want)"
        failed=1
    fi
}

check() {
    nft list table "$TABLE" >/dev/null 2>&1 || die "таблицы $TABLE нет — сначала install"
    failed=0
    echo "Проверка из контейнера worker:"
    expect closed 169.254.169.254 80   # метаданные облака
    expect closed 10.0.0.1 22          # частная сеть
    expect open db 5432                # база — соседний контейнер
    expect open redis 6379
    expect open ya.ru 443              # интернет и DNS
    expect open llm.api.cloud.yandex.net 443
    [[ $failed -eq 0 ]] || die "проверка не прошла; откат: $0 remove"
    echo "Всё как ожидалось."
}

case "${1:-}" in
    install)
        write_rules
        nft -c -f "$RULES" || die "правила не прошли проверку nft, ничего не применено"
        write_unit
        systemctl restart kronto-egress.service
        echo "Правила применены и включены при загрузке."
        check
        ;;
    check)
        check
        ;;
    remove)
        systemctl disable --now kronto-egress.service 2>/dev/null || true
        nft delete table "$TABLE" 2>/dev/null || true
        rm -f "$UNIT" "$RULES"
        systemctl daemon-reload
        echo "Правила сняты."
        ;;
    *)
        echo "usage: $0 install|check|remove" >&2
        exit 2
        ;;
esac
