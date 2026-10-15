# Samba в роли контроллера домена AD для живой проверки агента сетевых
# папок (agent/). Это Samba, а не Windows Server: протоколы те же (SMB 3,
# дескрипторы безопасности NTFS через vfs_acl_xattr, LDAP AD), но GPO, DFS
# и доверий между доменами здесь нет.
#
# APT_PROXY и секрет build_ca — только для сборки за HTTPS-прокси со своим
# сертификатом (песочница); без них — обычный apt.
FROM debian:trixie-slim

ARG APT_PROXY=""
RUN --mount=type=secret,id=build_ca,required=false,mode=0444 \
    set -eu; \
    if [ -n "$APT_PROXY" ]; then \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
        printf 'Acquire::https::Proxy "%s";\nAcquire::https::CAInfo "/run/secrets/build_ca";\n' "$APT_PROXY" \
            > /etc/apt/apt.conf.d/99proxy; \
    fi; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        samba-ad-dc samba-ad-provision smbclient ldb-tools krb5-user attr ca-certificates; \
    rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99proxy; \
    rm -f /etc/samba/smb.conf

COPY entrypoint.sh seed.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/seed.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
