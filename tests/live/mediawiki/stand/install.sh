#!/bin/bash
# Установка MediaWiki на SQLite при первом старте контейнера и запуск
# Apache. LocalSettings.php и база — в томе /var/www/data, настройки
# стенда (права, загрузки, Lockdown) — в /stand/settings.php: их правка
# действует без переустановки.
set -euo pipefail

DATA=/var/www/data
if [ ! -f "$DATA/LocalSettings.php" ]; then
    mkdir -p "$DATA"
    php maintenance/run.php install \
        --dbtype=sqlite --dbpath="$DATA" --dbname=wiki \
        --server="$WIKI_SERVER" --scriptpath="" --lang=ru \
        --pass="$WIKI_ADMIN_PASSWORD" --confpath="$DATA" \
        "Компания" Admin
    echo "require_once '/stand/settings.php';" >> "$DATA/LocalSettings.php"
    mkdir -p "$DATA/images"
    chown -R www-data:www-data "$DATA"
fi
ln -sf "$DATA/LocalSettings.php" /var/www/html/LocalSettings.php
# Файлы вики — в томе, а не в слое контейнера.
if [ ! -L /var/www/html/images ]; then
    rm -rf /var/www/html/images
    ln -s "$DATA/images" /var/www/html/images
fi
php maintenance/run.php update --quick > /dev/null
chown -R www-data:www-data "$DATA"
exec apache2-foreground
