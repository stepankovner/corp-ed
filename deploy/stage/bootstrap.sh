#!/usr/bin/env bash
# Первичная настройка тестового стенда на чистой Ubuntu 24.04 (docs/STAGE.md).
#
# Один раз, под root, на новом сервере — после того как A-запись DOMAIN
# указывает на его адрес:
#
#   curl -fsSLO https://raw.githubusercontent.com/stepankovner/corp-ed/main/deploy/stage/bootstrap.sh
#   DOMAIN=stage.krontoai.ru LETSENCRYPT_EMAIL=ops@krontoai.ru \
#   DEPLOY_PUBKEY="ssh-ed25519 AAAA… corp-ed-stage-deploy" bash bootstrap.sh
#
# Повторный запуск безопасен: каждый шаг проверяет, сделан ли он, и
# ничего не пересоздаёт (.env, сертификат, данные базы остаются).
set -euo pipefail

: "${DOMAIN:?DOMAIN — имя стенда; A-запись уже указывает на этот сервер}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL — почта для уведомлений о сертификате}"
: "${DEPLOY_PUBKEY:?DEPLOY_PUBKEY — открытый ключ, которым выкатывает GitHub Actions}"
REPO_URL="${REPO_URL:-https://github.com/stepankovner/corp-ed.git}"
IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/stepankovner}"
APP_DIR="${APP_DIR:-/opt/corp-ed}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
# Зеркало Docker Hub на случай, если hub.docker.com с сервера недоступен
# (у Timeweb — https://dockerhub.timeweb.cloud). Пусто — без зеркала.
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
    ca-certificates curl git openssl cron ufw unattended-upgrades \
    nginx certbot docker.io docker-compose-v2

log "Docker: ротация логов${REGISTRY_MIRROR:+, зеркало $REGISTRY_MIRROR}"
# Без ротации json-file растёт без предела и однажды заполняет диск
# (MONITORING-RESEARCH.md §1). Действует на контейнеры, созданные после.
mirrors=""
[[ -n "$REGISTRY_MIRROR" ]] && mirrors=", \"registry-mirrors\": [\"$REGISTRY_MIRROR\"]"
daemon_json="{\"log-driver\": \"json-file\", \"log-opts\": {\"max-size\": \"50m\", \"max-file\": \"5\"}$mirrors}"
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
# Ключ из GitHub Actions может только вызвать corp-ed-deploy: ни shell, ни
# проброса портов. Группа docker равна root, поэтому ключ — только так.
printf 'command="/usr/local/bin/corp-ed-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty %s\n' \
    "$DEPLOY_PUBKEY" > "$home/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"

log "Код в $APP_DIR"
if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone --quiet "$REPO_URL" "$APP_DIR"
fi
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

mkdir -p /etc/corp-ed
cat > /etc/corp-ed/stage.env <<EOF
# Настройки стенда для corp-ed-deploy и скриптов deploy/stage (bootstrap.sh).
APP_DIR=$APP_DIR
IMAGE_PREFIX=$IMAGE_PREFIX
DOMAIN=$DOMAIN
EOF
install -m 755 "$APP_DIR/deploy/stage/ssh-entry.sh" /usr/local/bin/corp-ed-deploy
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/log/corp-ed
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /var/backups/corp-ed

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
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl enable --now nginx >/dev/null
systemctl reload nginx

log "cron: purge, gaps, бэкап"
cat > /etc/cron.d/corp-ed <<EOF
# Регулярные задачи стенда (DEPLOY.md §6, §8). Ставит bootstrap.sh.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
10 3 * * * $DEPLOY_USER cd $APP_DIR && docker compose -f compose.yaml run --rm --no-deps api python -m corp_ed.cli purge >> /var/log/corp-ed/cron.log 2>&1
30 3 * * * $DEPLOY_USER cd $APP_DIR && docker compose -f compose.yaml run --rm --no-deps api python -m corp_ed.cli gaps --all >> /var/log/corp-ed/cron.log 2>&1
0 4 * * * $DEPLOY_USER $APP_DIR/deploy/stage/backup.sh >> /var/log/corp-ed/cron.log 2>&1
EOF
chmod 644 /etc/cron.d/corp-ed

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
    workflow «Deploy»; или вручную:
      sudo -u $DEPLOY_USER SSH_ORIGINAL_COMMAND="deploy <sha>" /usr/local/bin/corp-ed-deploy
EOF
