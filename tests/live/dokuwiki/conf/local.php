<?php
/*
 * Настройки стенда DokuWiki для живой проверки вида `dokuwiki`: ACL,
 * пользователи из users.auth.php, суперпользователи — группа admin,
 * удалённый API открыт вошедшим (группа user).
 */
$conf['title'] = 'Kronto stand';
$conf['lang'] = 'ru';
$conf['useacl'] = 1;
$conf['authtype'] = 'authplain';
$conf['superuser'] = '@admin';
$conf['disableactions'] = 'register';
$conf['useheading'] = '1';
$conf['remote'] = 1;
$conf['remoteuser'] = '@user';
