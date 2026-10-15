#!/usr/bin/with-contenv bash
# Песочница без IPv6: nginx образа linuxserver не стартует с `listen [::]`.
# Скрипты из /custom-cont-init.d выполняются до запуска сервисов.
sed -i '/listen \[::\]/d' /config/nginx/site-confs/default.conf
