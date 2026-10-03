#!/usr/bin/env bash
# Свой runner GitHub Actions на чистой Ubuntu 24.04 (docs/CI-RUNNER.md).
#
# Один раз, под root, на отдельной ВМ: на ней только runner, данных стенда
# нет. Токен регистрации (GitHub → Settings → Actions → Runners → New
# self-hosted runner, живёт час) — первой строкой stdin, не в аргументах и
# не в переменных: там он попал бы в историю shell и в ps.
#
#   bash setup.sh                                          # спросит токен, ввод скрыт
#   printf '%s\n' "$TOKEN" | ssh root@<IP> bash /root/setup.sh
#
# Повторный запуск безопасен: каждый шаг проверяет, сделан ли он;
# настроенный экземпляр runner (есть .runner) не трогается — если все
# настроены, токен не нужен.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/stepankovner/corp-ed}"
# Экземпляров — столько заданий идёт одновременно. На 4 vCPU / 8 ГБ — два.
RUNNERS="${RUNNERS:-2}"
RUNNER_LABEL="${RUNNER_LABEL:-kronto}"
RUNNER_USER=runner
RUNNER_HOME=/home/runner
# Релиз actions/runner для первой установки, дальше runner обновляется сам.
# SHA-256 архива linux-x64 — сверять с разделом «SHA-256 Checksums» в
# описании релиза на github.com/actions/runner/releases.
RUNNER_VERSION=2.337.0
RUNNER_SHA256=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613
# Зеркало Docker Hub (например, https://mirror.gcr.io). Пусто — без зеркала.
REGISTRY_MIRROR="${REGISTRY_MIRROR:-}"
SWAP_SIZE="${SWAP_SIZE:-4G}"
# Диск занят больше, % — внеочередная чистка Docker (kronto-ci-clean guard).
DISK_LIMIT="${DISK_LIMIT:-80}"

# Библиотеки Chromium для Playwright. На машинах GitHub их ставит
# `playwright install --with-deps` через sudo; у runner sudo нет, поэтому —
# здесь, а задание e2e на своём runner ставит только браузер. Список —
# Playwright 1.63, ubuntu24.04-x64, группы tools и chromium.
PLAYWRIGHT_DEPS=(
    xvfb fonts-noto-color-emoji fonts-unifont libfontconfig1 libfreetype6
    xfonts-cyrillic xfonts-scalable fonts-liberation fonts-ipafont-gothic
    fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf
    libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64
    libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4
    libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1
    libxext6 libxfixes3 libxkbcommon0 libxrandr2
)

log() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "запускать под root" >&2; exit 1; }
grep -qx 'VERSION_ID="24.04"' /etc/os-release || { echo "нужна Ubuntu 24.04: имена пакетов — под неё" >&2; exit 1; }
[[ "$RUNNERS" =~ ^[1-9]$ ]] || { echo "RUNNERS: $RUNNERS — число от 1 до 9" >&2; exit 1; }
[[ "$REPO_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "REPO_URL: $REPO_URL — не адрес репозитория GitHub" >&2; exit 1; }
[[ "$RUNNER_LABEL" =~ ^[a-z0-9-]+$ ]] || { echo "RUNNER_LABEL: $RUNNER_LABEL — латиница, цифры, дефис" >&2; exit 1; }
# Адрес зеркала подставляется в daemon.json.
[[ "$REGISTRY_MIRROR" =~ ^(https://[A-Za-z0-9.:-]+)?$ ]] || { echo "REGISTRY_MIRROR: $REGISTRY_MIRROR — не адрес https" >&2; exit 1; }
[[ "$DISK_LIMIT" =~ ^[1-9][0-9]$ ]] || { echo "DISK_LIMIT: $DISK_LIMIT — процент от 10 до 99" >&2; exit 1; }

log "Токен регистрации"
# Экземпляр настроен, если есть .runner — его пишет config.sh.
pending=()
for i in $(seq 1 "$RUNNERS"); do
    [[ -f "$RUNNER_HOME/actions-runner-$i/.runner" ]] || pending+=("$i")
done
token=""
if (( ${#pending[@]} )); then
    [[ -t 0 ]] && printf 'токен регистрации (ввод скрыт): ' >&2
    IFS= read -rs token || true
    [[ -t 0 ]] && echo >&2
    token="${token//[[:space:]]/}"
    [[ "$token" =~ ^[A-Za-z0-9_-]{20,}$ ]] || { echo "нет токена: первой строкой stdin (docs/CI-RUNNER.md, шаг 3.4)" >&2; exit 1; }
    echo "нужен экземплярам: ${pending[*]}"
else
    echo "все экземпляры (1–$RUNNERS) настроены — токен не нужен"
fi
# Дальше stdin никому не нужен: apt и config.sh не прочтут остаток ввода.
exec </dev/null

log "Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
# Чего нет в чистой Ubuntu, но есть в образе ubuntu-24.04 у GitHub и нужно
# заданиям (CI-RUNNER.md §2): git — checkout и `git diff` в check:api; psql —
# задание migrations; python3 — Stage resume и выбор портов в e2e; zstd —
# сжатие кэша actions/cache и бандл CodeQL; build-essential — на случай
# пакета без готовой сборки (сейчас таких нет). libicu, libkrb5, liblttng-ust,
# libssl, zlib — сам runner (.NET), как в его bin/installdependencies.sh.
apt-get install -y -q --no-install-recommends \
    ca-certificates curl gnupg git openssh-client openssl cron ufw unattended-upgrades \
    jq python3 postgresql-client zstd xz-utils unzip build-essential \
    libicu74 libkrb5-3 liblttng-ust1t64 libssl3t64 zlib1g \
    "${PLAYWRIGHT_DEPS[@]}"

log "Автообновления безопасности"
# В образе Ubuntu обычно уже включены; файл — на случай образа без них.
# Docker из репозитория Docker они не обновляют — вручную (CI-RUNNER.md §5).
printf '%s\n' 'APT::Periodic::Update-Package-Lists "1";' 'APT::Periodic::Unattended-Upgrade "1";' \
    > /etc/apt/apt.conf.d/20auto-upgrades

log "Время — UTC, как у машин GitHub"
# Тесты с датами и cron ниже — в том же поясе, что на GitHub.
[[ "$(timedatectl show -p Timezone --value)" == "Etc/UTC" ]] || timedatectl set-timezone Etc/UTC

log "Журнал: не больше 1 ГБ"
mkdir -p /etc/systemd/journald.conf.d
journald_conf="[Journal]
Storage=persistent
SystemMaxUse=1G
MaxRetentionSec=14day"
if [[ "$(cat /etc/systemd/journald.conf.d/kronto-ci.conf 2>/dev/null)" != "$journald_conf" ]]; then
    printf '%s\n' "$journald_conf" > /etc/systemd/journald.conf.d/kronto-ci.conf
    systemctl restart systemd-journald
fi

log "Swap $SWAP_SIZE"
# Два задания на 8 ГБ: CodeQL и сборка образа берут по нескольку гигабайт.
# Swap — запас против OOM killer, а не рабочая память.
if [[ -z "$(swapon --show --noheadings)" ]]; then
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Docker"
# Из репозитория Docker — свежий Engine с buildx и compose. Если
# download.docker.com с сервера недоступен (бывает из РФ, STAGE.md §4.4) —
# из архива Ubuntu, как на стенде. Уже установленный не трогаем: docker-ce
# и docker.io конфликтуют, а обновление перезапустит Docker посреди заданий.
docker_key=$(mktemp)
if dpkg -s docker-ce >/dev/null 2>&1 || dpkg -s docker.io >/dev/null 2>&1; then
    echo "уже установлен"
elif curl -fsSL --max-time 30 https://download.docker.com/linux/ubuntu/gpg -o "$docker_key"; then
    # Отпечаток ключа — из документации Docker («Install Docker Engine on Ubuntu»).
    fpr=$(gpg --show-keys --with-colons "$docker_key" | awk -F: '$1 == "fpr" { print $10; exit }')
    [[ "$fpr" == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || { echo "ключ Docker: чужой отпечаток $fpr" >&2; exit 1; }
    install -D -m 644 "$docker_key" /etc/apt/keyrings/docker.asc
    printf '%s\n' 'Types: deb' 'URIs: https://download.docker.com/linux/ubuntu' 'Suites: noble' \
        'Components: stable' 'Signed-By: /etc/apt/keyrings/docker.asc' > /etc/apt/sources.list.d/docker.sources
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    echo "download.docker.com недоступен — Docker из архива Ubuntu" >&2
    apt-get install -y -q --no-install-recommends docker.io docker-buildx docker-compose-v2
fi
rm -f "$docker_key"

log "Docker: порты — только на 127.0.0.1${REGISTRY_MIRROR:+, зеркало $REGISTRY_MIRROR}"
# Docker публикует порты в обход ufw, а сервисы заданий (postgres, redis)
# публикуются на порт хоста — без "ip" их было бы видно из интернета.
# log-driver local — с ротацией: json-file по умолчанию растёт без предела.
mirrors=""
[[ -n "$REGISTRY_MIRROR" ]] && mirrors=", \"registry-mirrors\": [\"$REGISTRY_MIRROR\"]"
daemon_json="{\"ip\": \"127.0.0.1\", \"log-driver\": \"local\"$mirrors}"
if [[ "$(cat /etc/docker/daemon.json 2>/dev/null)" != "$daemon_json" ]]; then
    mkdir -p /etc/docker
    printf '%s\n' "$daemon_json" > /etc/docker/daemon.json
    systemctl restart docker
fi
systemctl enable --now docker >/dev/null

log "Пользователь $RUNNER_USER"
# Задания идут под ним, без sudo: что им нужно от root, ставит этот скрипт.
# Группа docker равна root (без неё нет сервисов заданий и сборки образов) —
# поэтому runner живёт на отдельной ВМ, где больше ничего нет.
id "$RUNNER_USER" >/dev/null 2>&1 || useradd --create-home --home-dir "$RUNNER_HOME" --shell /bin/bash "$RUNNER_USER"
usermod -aG docker "$RUNNER_USER"

log "Файрвол: снаружи только 22"
# Runner сам ходит к GitHub; входящих, кроме SSH, ему не нужно.
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw --force enable >/dev/null

log "SSH: вход только по ключу"
# Как на стенде (deploy/stage/bootstrap.sh): пароль root из панели Timeweb
# остаётся для консоли в панели, по SSH — только ключ. 00-… читается раньше
# 50-cloud-init.conf, а sshd берёт первое значение. Нет ключа у root — не
# трогаем: кто вошёл по паролю, потерял бы SSH. KexAlgorithms —
# постквантовый обмен первым (STAGE.md §4.1).
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
    systemctl try-reload-or-restart ssh
else
    echo "у root нет SSH-ключа — вход по паролю оставлен (CI-RUNNER.md, шаг 3.1)" >&2
fi

log "Чистка Docker: раз в неделю и при заполненном диске"
# Машина GitHub после задания исчезает, наша — нет: образы, кэш сборки и
# анонимные тома postgres из сервисов (runner удаляет контейнер без тома)
# копятся. Возраст — фильтр until: образ бегущего задания моложе часа.
cat > /usr/local/sbin/kronto-ci-clean <<'EOF'
#!/usr/bin/env bash
# Чистка Docker на runner (ставит deploy/ci-runner/setup.sh).
#   weekly — всё неиспользуемое старше недели и тома без контейнеров;
#   guard  — то же старше суток, если диск занят больше DISK_LIMIT %;
#            не хватило — старше часа.
set -euo pipefail
limit="${DISK_LIMIT:-80}"
used() { df --output=pcent / | tail -1 | tr -dc 0-9; }
prune() { docker system prune -af --filter "until=$1" >/dev/null; docker volume prune -f >/dev/null; }
case "${1:-}" in
    weekly) prune 168h ;;
    guard)
        (( $(used) >= limit )) || exit 0
        echo "диск занят на $(used)% (порог $limit%) — чистка"
        prune 24h
        (( $(used) < limit )) || prune 1h ;;
    *) echo "usage: $0 weekly|guard" >&2; exit 2 ;;
esac
echo "$1: диск занят на $(used)%"
EOF
chmod 755 /usr/local/sbin/kronto-ci-clean
cat > /etc/cron.d/kronto-ci <<EOF
# Чистка Docker на runner (docs/CI-RUNNER.md §5). Ставит setup.sh.
# Вывод — в журнал: journalctl -t kronto-ci-clean.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DISK_LIMIT=$DISK_LIMIT
17 4 * * 0 root systemd-cat -t kronto-ci-clean kronto-ci-clean weekly
*/15 * * * * root systemd-cat -t kronto-ci-clean kronto-ci-clean guard
EOF
chmod 644 /etc/cron.d/kronto-ci

log "actions/runner $RUNNER_VERSION"
tarball="/var/cache/kronto-ci/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"
if (( ${#pending[@]} )) && ! echo "$RUNNER_SHA256  $tarball" | sha256sum -c --status 2>/dev/null; then
    install -d -m 755 /var/cache/kronto-ci
    curl -fsSL --retry 3 -o "$tarball.part" \
        "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"
    echo "$RUNNER_SHA256  $tarball.part" | sha256sum -c --quiet \
        || { rm -f "$tarball.part"; echo "архив runner: SHA-256 не совпал" >&2; exit 1; }
    mv "$tarball.part" "$tarball"
fi

for i in $(seq 1 "$RUNNERS"); do
    dir="$RUNNER_HOME/actions-runner-$i"
    name="kronto-ci-$i"
    log "Runner $name"
    if [[ ! -f "$dir/.runner" ]]; then
        install -d -m 755 -o "$RUNNER_USER" -g "$RUNNER_USER" "$dir"
        [[ -x "$dir/config.sh" ]] || runuser -u "$RUNNER_USER" -- tar -xzf "$tarball" -C "$dir"
        # Токен — окружением: его читает сам runner (ACTIONS_RUNNER_INPUT_TOKEN)
        # и сразу убирает; аргументы видны всем в ps. LANG попадёт в .env
        # runner — как C.UTF-8 на машинах GitHub. --replace: ВМ пересоздали —
        # экземпляр с тем же именем в GitHub заменяется.
        (
            cd "$dir"
            ACTIONS_RUNNER_INPUT_TOKEN="$token" LANG=C.UTF-8 runuser -u "$RUNNER_USER" -- \
                ./config.sh --unattended --url "$REPO_URL" --name "$name" \
                --labels "$RUNNER_LABEL" --work _work --replace
        )
    fi
    # .service пишет svc.sh install: в нём имя юнита systemd.
    [[ -f "$dir/.service" ]] || ( cd "$dir" && ./svc.sh install "$RUNNER_USER" )
    ( cd "$dir" && ./svc.sh start >/dev/null )
    echo "$(cat "$dir/.service"): $(systemctl is-active "$(cat "$dir/.service")")"
done

log "Готово"
cat <<EOF
Дальше (docs/CI-RUNNER.md, шаг 3.5):
  - GitHub → Settings → Actions → Runners: kronto-ci-1…$RUNNERS в статусе Idle;
  - Settings → Secrets and variables → Actions → Variables: RUNS_ON =
      ["self-hosted","$RUNNER_LABEL"]
    — и все workflow идут сюда. Удалить переменную — снова машины GitHub.
EOF
