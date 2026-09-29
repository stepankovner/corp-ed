#!/usr/bin/env bash
# corp-ed-deploy: принудительная команда ключа выкатки (authorized_keys
# пользователя deploy, ставит bootstrap.sh). С этим ключом можно только
# выкатить коммит из ветки этого репозитория — ни shell, ни проброса
# портов. После изменения файла — перезапустить bootstrap.sh.
#
# Вход: SSH_ORIGINAL_COMMAND="deploy <sha коммита, 40 символов>".
set -euo pipefail
# shellcheck source=/dev/null
. /etc/corp-ed/stage.env

read -r action sha extra <<<"${SSH_ORIGINAL_COMMAND:-}" || true
if [[ "${action:-}" != deploy || ! "${sha:-}" =~ ^[0-9a-f]{40}$ || -n "${extra:-}" ]]; then
    echo "usage: deploy <commit sha>" >&2
    exit 64
fi

cd "$APP_DIR"
# Только коммиты из веток origin: по SHA GitHub отдаёт и коммиты форков,
# а с ними — чужие compose.yaml и deploy.sh на нашем сервере.
git fetch --quiet --prune origin '+refs/heads/*:refs/remotes/origin/*'
if ! git branch --remotes --contains "$sha" 2>/dev/null | grep -q .; then
    echo "commit $sha is not on any branch of origin" >&2
    exit 65
fi
git checkout --quiet --force --detach "$sha"
# Дальше — deploy.sh из выкатываемого коммита, а не из прошлого.
exec "$APP_DIR/deploy/stage/deploy.sh" "$sha"
