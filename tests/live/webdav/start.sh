#!/bin/sh
# Apache httpd с mod_dav и TLS для стенда WebDAV (NAS): модули включаются в
# стандартном httpd.conf образа, пользователи — в htpasswd при старте (пароль
# выдуманный, из compose.yaml), хранилище — том /dav.
set -eu
conf=/usr/local/apache2/conf/httpd.conf
sed -i \
  -e 's/^#LoadModule \(dav_module\|dav_fs_module\|ssl_module\|socache_shmcb_module\)/LoadModule \1/' \
  -e 's/^Listen 80$/Listen 443/' \
  "$conf"
echo "Include /stand/dav.conf" >> "$conf"
htpasswd -bc /usr/local/apache2/users ivan "$STAND_PASSWORD"
htpasswd -b /usr/local/apache2/users maria "$STAND_PASSWORD"
mkdir -p /dav/files /dav/locks
chown -R www-data:www-data /dav
exec httpd-foreground
