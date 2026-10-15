#!/bin/sh
# Первый запуск — провизия домена KRONTO.TEST и общий ресурс docs с
# правами NTFS в xattr (vfs_acl_xattr); дальше — samba в переднем плане.
# Пароль администратора выдуманный, только для стенда.
set -eu

REALM="${SMB_REALM:-KRONTO.TEST}"
DOMAIN="${SMB_DOMAIN:-KRONTO}"
ADMIN_PASSWORD="${SMB_ADMIN_PASSWORD:-Kronto-Stand-Admin-1}"

if [ ! -f /var/lib/samba/private/sam.ldb ]; then
    rm -f /etc/samba/smb.conf
    samba-tool domain provision \
        --realm="$REALM" --domain="$DOMAIN" \
        --server-role=dc --dns-backend=SAMBA_INTERNAL \
        --adminpass="$ADMIN_PASSWORD" \
        --option="dns forwarder = 127.0.0.11"
    mkdir -p /srv/docs
    cat >> /etc/samba/smb.conf <<CONF

[docs]
	path = /srv/docs
	read only = no
	vfs objects = dfs_samba4 acl_xattr
CONF
    cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
fi

# CA стенда (самоподписанный, samba создаёт его при первом запуске) — для
# LDAPS в тестах: копия в /export, как только появится.
if [ -d /export ]; then
    (
        for _ in $(seq 1 120); do
            if [ -f /var/lib/samba/private/tls/ca.pem ]; then
                cp /var/lib/samba/private/tls/ca.pem /export/ca.pem
                chmod 0644 /export/ca.pem
                break
            fi
            sleep 1
        done
    ) &
fi

exec samba --foreground --no-process-group --debug-stdout
