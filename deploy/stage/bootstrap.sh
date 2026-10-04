#!/usr/bin/env bash
# Первичная настройка тестового стенда на чистой Ubuntu 24.04 (docs/STAGE.md).
#
# Один раз, под root, на новом сервере — после того как A-запись DOMAIN
# указывает на его адрес. Репозиторий приватный: скрипт копируется руками
# (GitHub → файл → Raw, STAGE.md §4.4), затем:
#
#   DOMAIN=stage.krontoai.ru LETSENCRYPT_EMAIL=ops@krontoai.ru \
#   DEPLOY_PUBKEY="ssh-ed25519 AAAA… kronto-stage-deploy" bash bootstrap.sh
#
# Первый запуск остановится на шаге «Код»: он напечатает ключ сервера для
# GitHub (Deploy key, только чтение). Добавить ключ и запустить ещё раз.
#
# Повторный запуск безопасен: каждый шаг проверяет, сделан ли он, и
# ничего не пересоздаёт (.env, сертификат, данные базы остаются).
set -euo pipefail

: "${DOMAIN:?DOMAIN — имя стенда; A-запись уже указывает на этот сервер}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL — почта для уведомлений о сертификате}"
: "${DEPLOY_PUBKEY:?DEPLOY_PUBKEY — открытый ключ, которым выкатывает GitHub Actions}"
# Приватный репозиторий — по SSH ключом сервера (deploy key, только чтение).
REPO_URL="${REPO_URL:-git@github.com:stepankovner/corp-ed.git}"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/stepankovner}"
APP_DIR="${APP_DIR:-/opt/kronto}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
# Зеркало Docker Hub на случай, если hub.docker.com с сервера недоступен
# (например, https://mirror.gcr.io). Пусто — без зеркала.
REGISTRY_MIRROR="${REGISTRY_MIRROR:-}"
SWAP_SIZE="${SWAP_SIZE:-4G}"

log() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "запускать под root" >&2; exit 1; }
# Имя подставляется в конфиг nginx и .env — только буквы, цифры, точки, дефисы.
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || { echo "DOMAIN: $DOMAIN — не имя хоста" >&2; exit 1; }
[[ "$DEPLOY_PUBKEY" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "DEPLOY_PUBKEY — не открытый ключ OpenSSH" >&2; exit 1; }

log "Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
# Docker и compose — из архива Ubuntu: он доступен из РФ через зеркала,
# в отличие от download.docker.com. Нужен compose v2 (docker compose).
apt-get install -y -q --no-install-recommends \
    ca-certificates curl git openssh-client openssl cron ufw unattended-upgrades \
    nginx certbot docker.io docker-compose-v2

log "Журнал: 14 дней, логи Docker — в journald"
# Логи контейнеров — в journald (решение 30.09: хранить 14 дней). Оттуда
# их читает Alloy для Grafana (deploy/monitoring) — без доступа к сокету
# Docker; `docker logs` работает как раньше. Файлы журнала — по суткам,
# чтобы срок соблюдался с точностью до дня; общий объём — не больше 2 ГБ.
# RateLimit выключен: иначе journald молча выбрасывает всплеск логов.
mkdir -p /etc/systemd/journald.conf.d
journald_conf="[Journal]
Storage=persistent
SystemMaxUse=2G
MaxRetentionSec=14day
MaxFileSec=1day
RateLimitIntervalSec=0"
if [[ "$(cat /etc/systemd/journald.conf.d/kronto.conf 2>/dev/null)" != "$journald_conf" ]]; then
    printf '%s\n' "$journald_conf" > /etc/systemd/journald.conf.d/kronto.conf
    systemctl restart systemd-journald
fi

log "Docker: логи в journald${REGISTRY_MIRROR:+, зеркало $REGISTRY_MIRROR}"
# Действует на контейнеры, созданные после.
mirrors=""
[[ -n "$REGISTRY_MIRROR" ]] && mirrors=", \"registry-mirrors\": [\"$REGISTRY_MIRROR\"]"
daemon_json="{\"log-driver\": \"journald\"$mirrors}"
if [[ "$(cat /etc/docker/daemon.json 2>/dev/null)" != "$daemon_json" ]]; then
    mkdir -p /etc/docker
    printf '%s\n' "$daemon_json" > /etc/docker/daemon.json
    systemctl restart docker
fi
systemctl enable --now docker >/dev/null

log "Swap $SWAP_SIZE"
# Разбор PDF в песочнице берёт до 1,5 ГБ (ingest/extract_worker.py): swap —
# запас против OOM killer, а не рабочая память.
if [[ -z "$(swapon --show --noheadings)" ]]; then
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Пользователь $DEPLOY_USER и ключ выкатки"
id "$DEPLOY_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$DEPLOY_USER"
usermod -aG docker "$DEPLOY_USER"
home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
# Ключ из GitHub Actions может только вызвать kronto-deploy: ни shell, ни
# проброса портов. Группа docker равна root, поэтому ключ — только так.
printf 'command="/usr/local/bin/kronto-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty %s\n' \
    "$DEPLOY_PUBKEY" > "$home/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"

log "Ключ сервера для чтения репозитория"
# Репозиторий приватный (с 30.09): код и каждую выкатку сервер забирает
# своим ключом — deploy key в GitHub, только чтение. Ключ хоста GitHub
# закреплён, а не принят на веру при первом подключении (docs.github.com,
# «GitHub's SSH key fingerprints»: SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU).
github_key="$home/.ssh/github_read"
if [[ ! -f "$github_key" ]]; then
    sudo -u "$DEPLOY_USER" ssh-keygen -q -t ed25519 -N "" \
        -C "kronto-stage-read@$DOMAIN" -f "$github_key"
fi
printf 'Host github.com\n    IdentityFile %s\n    IdentitiesOnly yes\n' "$github_key" > "$home/.ssh/config"
echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" \
    > "$home/.ssh/known_hosts"
chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/config" "$home/.ssh/known_hosts"
chmod 600 "$home/.ssh/config" "$home/.ssh/known_hosts"

log "Код в $APP_DIR"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR"
if [[ ! -d "$APP_DIR/.git" ]]; then
    if ! sudo -u "$DEPLOY_USER" git clone --quiet "$REPO_URL" "$APP_DIR"; then
        cat >&2 <<EOF

Код не скачался: репозиторий приватный, у сервера пока нет доступа.
GitHub → репозиторий → Settings → Deploy keys → Add deploy key:
  Title — kronto-stage, Key — строка ниже, «Allow write access» НЕ ставить.

$(cat "$github_key.pub")

Затем запустите bootstrap.sh ещё раз с теми же переменными.
EOF
        exit 2
    fi
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

mkdir -p /etc/kronto
cat > /etc/kronto/stage.env <<EOF
# Настройки стенда для kronto-deploy и скриптов deploy/stage (bootstrap.sh).
APP_DIR=$APP_DIR
IMAGE_PREFIX=$IMAGE_PREFIX
DOMAIN=$DOMAIN
EOF
install -m 755 "$APP_DIR/deploy/stage/ssh-entry.sh" /usr/local/bin/kronto-deploy
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/log/kronto
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/backups/kronto
# Пароль компании для сквозной проверки (check.sh) — только здесь.
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/lib/kronto
# Отметки ночных задач для node-exporter (cron-run.sh, «мёртвая рука»).
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/lib/node_exporter/textfile

log "Мониторинг: настройки"
# Пароль Grafana — один раз, дальше не меняется. Файл читают root и
# deploy (deploy.sh поднимает мониторинг при каждой выкатке).
if [[ ! -f /etc/kronto/monitoring.env ]]; then
    (
        umask 027
        printf '%s\n' \
            "# Мониторинг стенда (deploy/monitoring). Grafana: https://$DOMAIN/grafana/, вход admin." \
            "DOMAIN=$DOMAIN" \
            "GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)" \
            "APP_NETWORK=$(basename "$APP_DIR")_default" \
            "PUBLIC_TARGETS_FILE=/etc/kronto/public-targets.yml" \
            > /etc/kronto/monitoring.env
    )
    chgrp "$DEPLOY_USER" /etc/kronto/monitoring.env
fi
printf -- '- targets: ["https://%s/health/ready"]\n' "$DOMAIN" > /etc/kronto/public-targets.yml
chmod 644 /etc/kronto/public-targets.yml

log ".env"
if [[ ! -f "$APP_DIR/.env" ]]; then
    ( umask 077; DOMAIN="$DOMAIN" bash "$APP_DIR/deploy/stage/make-env.sh" "$APP_DIR/.env.example" > "$APP_DIR/.env" )
    chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"
    env_created=1
fi

log "Файрвол: 22, 80, 443"
# Порты api и web в compose.yaml опубликованы только на 127.0.0.1 — Docker
# обходит ufw, поэтому это важно (DEPLOY.md §5).
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null

log "SSH: вход только по ключу"
# Selectel пускает root по SSH и с паролем из панели — его подбирают боты.
# Пароль остаётся для консоли в панели Selectel. 00-… читается раньше
# 50-cloud-init.conf, а sshd берёт первое значение. Нет ключа у root — не
# трогаем: кто вошёл по паролю, потерял бы SSH.
# KexAlgorithms: набор OpenSSH по умолчанию, постквантовый обмен первым. С
# сервером Selectel ssh 10.x договорился о непостквантовом и предупредил
# (02.10); от чего так в образе — не выяснено, строка закрывает любой случай.
sshd_drop=/etc/ssh/sshd_config.d/00-kronto.conf
if grep -qsE '(ssh-(ed25519|rsa)|ecdsa-sha2-[a-z0-9]+) AAAA' /root/.ssh/authorized_keys; then
    printf '%s\n' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'PermitRootLogin prohibit-password' \
        'KexAlgorithms ^sntrup761x25519-sha512@openssh.com' > "$sshd_drop"
    # Проверке sshd -t нужен /run/sshd, а его держит только запущенная
    # служба: после обновления openssh-server (шаг «Пакеты») её может не
    # быть — sshd поднимется по первому подключению (ssh.socket).
    install -d -m 755 /run/sshd
    sshd -t || { rm -f "$sshd_drop"; echo "sshd не принял настройку — оставлена прежняя" >&2; exit 1; }
    # Ubuntu 24.04 поднимает sshd по первому подключению (ssh.socket):
    # не запущен — новую настройку прочтёт при старте.
    systemctl try-reload-or-restart ssh
else
    echo "у root нет SSH-ключа — вход по паролю оставлен (STAGE.md, шаг 4.1)" >&2
fi

log "TLS для $DOMAIN"
if [[ ! -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    # standalone: nginx на время выпуска и продления останавливается
    # (секунды раз в ~60 дней) — для стенда проще, чем webroot.
    certbot certonly --standalone --non-interactive --agree-tos \
        -m "$LETSENCRYPT_EMAIL" -d "$DOMAIN" \
        --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
fi

log "nginx"
# Боевой конфиг, только с именем стенда: проверяем ровно то, что пойдёт в бой.
sed "s/app\.krontoai\.ru/$DOMAIN/g" "$APP_DIR/deploy/nginx/kronto.conf" > /etc/nginx/sites-available/kronto
ln -sf /etc/nginx/sites-available/kronto /etc/nginx/sites-enabled/kronto
# Grafana стенда (deploy/monitoring) — под паролем, по тому же TLS.
install -d /etc/nginx/kronto.d
install -m 644 "$APP_DIR/deploy/monitoring/nginx-grafana.conf" /etc/nginx/kronto.d/grafana.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl enable --now nginx >/dev/null
systemctl reload nginx

log "cron: purge, gaps, бэкап"
cat > /etc/cron.d/kronto <<EOF
# Регулярные задачи стенда (DEPLOY.md §6, §8). Ставит bootstrap.sh.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# cron-run.sh отмечает успех для мониторинга: нет отметки больше 26 часов —
# тревога (deploy/monitoring/prometheus/rules/kronto.yml).
10 3 * * * $DEPLOY_USER cd $APP_DIR && deploy/stage/cron-run.sh purge docker compose -f compose.yaml run --rm --no-deps api python -m corp_ed.cli purge >> /var/log/kronto/cron.log 2>&1
30 3 * * * $DEPLOY_USER cd $APP_DIR && deploy/stage/cron-run.sh gaps docker compose -f compose.yaml run --rm --no-deps api python -m corp_ed.cli gaps --all >> /var/log/kronto/cron.log 2>&1
0 4 * * * $DEPLOY_USER cd $APP_DIR && deploy/stage/cron-run.sh backup deploy/stage/backup.sh >> /var/log/kronto/cron.log 2>&1
EOF
chmod 644 /etc/cron.d/kronto

log "Готово"
cat <<EOF
Дальше (docs/STAGE.md, шаги 5–8):
EOF
if [[ -n "${env_created:-}" ]]; then
    cat <<EOF
  - впишите YC_FOLDER_ID и YC_API_KEY в $APP_DIR/.env (остальные секреты
    сгенерированы; файл 600, владелец $DEPLOY_USER);
EOF
fi
cat <<EOF
  - ключ хоста для GitHub (переменная STAGE_SSH_KNOWN_HOSTS, STAGE_HOST=$DOMAIN):
      $DOMAIN $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)
  - включите выкатку: переменная репозитория STAGE_ENABLED=true и Run
    workflow «Deploy». Проверка стенда вручную:
      sudo -u $DEPLOY_USER SSH_ORIGINAL_COMMAND=check /usr/local/bin/kronto-deploy
  - мониторинг поднимется с первой выкаткой: https://$DOMAIN/grafana/,
    вход admin, пароль — sudo grep GRAFANA /etc/kronto/monitoring.env
EOF
