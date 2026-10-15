#!/bin/sh
# Засев стенда (только на чистом стенде): пользователи и группы домена,
# папки и файлы ресурса docs с правами NTFS. Права ставятся по SMB
# (smbclient/smbcacls от имени администратора), как это сделал бы
# администратор Windows: файлы создаются после прав на папки, и сервер сам
# вычисляет унаследованные ACE (флаг INHERITED_ACE). Пароли выдуманные.
#
#   docker compose -f tests/live/smb/compose.yaml exec dc seed.sh
#
# Что ожидается от агента — EXPECTED в agent/tests/live/test_smb_live.py.
set -eu

ADMIN="KRONTO\\Administrator%${SMB_ADMIN_PASSWORD:-Kronto-Stand-Admin-1}"
PASS="Kronto-Stand-User-1"
SHARE="//localhost/docs"
D="KRONTO"

user() { # login mail
    if [ -n "$2" ]; then
        samba-tool user create "$1" "$PASS" --mail-address="$2" >/dev/null
    else
        samba-tool user create "$1" "$PASS" >/dev/null
    fi
}

# Пользователи: 4 с почтой, отключённый с почтой, включённый без почты
# (почта только в UPN) и учётка агента (только чтение, без почты).
user anna anna@example.com
user boris boris@example.com
user vera vera@example.com
user gleb gleb@example.com
user dmitry dmitry@example.com
samba-tool user disable dmitry >/dev/null
user egor ""
user svc-kronto ""

# Группы: hr-leads вложена в hr; отключённый dmitry — в hr.
samba-tool group add hr >/dev/null
samba-tool group add hr-leads >/dev/null
samba-tool group add kronto-readers >/dev/null
samba-tool group addmembers hr anna,dmitry,hr-leads >/dev/null
samba-tool group addmembers hr-leads boris >/dev/null
samba-tool group addmembers kronto-readers svc-kronto >/dev/null

sid() { samba-tool "$1" show "$2" --attributes=objectSid | sed -n 's/^objectSid: //p'; }
READERS_SID=$(sid group kronto-readers)
HR_SID=$(sid group hr)
VERA_SID=$(sid user vera)

smb() { smbclient "$SHARE" -U "$ADMIN" -m SMB3 -c "$1" >/dev/null 2>&1; }
acl() { smbcacls "$SHARE" "$1" -U "$ADMIN" "$2" "$3" >/dev/null; }
ntacl() { samba-tool ntacl set --service=docs "$1" "/srv/docs/$2"; }

# Корень ресурса: защищён от наследования, полный доступ — SYSTEM и
# Domain Admins, чтение всего дерева — группе агента (без почт: в списки
# прав не попадает).
ntacl "O:DAG:DUD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;DA)(A;OICI;0x1200a9;;;${READERS_SID})" ""

# Папки первого уровня: свои ACE плюс унаследованные от корня (ID) и флаг
# AI — так их записывает Windows, и тогда сервер помечает ACE файлов,
# созданных ниже, как унаследованные. Общая — все пользователи домена;
# Кадры — группа hr; Проекты — все, кроме vera (явный запрет на папке).
smb 'mkdir "Общая"; mkdir "Кадры"; mkdir "Проекты"'
ROOT_ID="(A;OICIID;FA;;;SY)(A;OICIID;FA;;;DA)(A;OICIID;0x1200a9;;;${READERS_SID})"
ntacl "O:DAG:DUD:AI(A;OICI;0x1200a9;;;DU)${ROOT_ID}" "Общая"
ntacl "O:DAG:DUD:AI(A;OICI;0x1200a9;;;${HR_SID})${ROOT_ID}" "Кадры"
ntacl "O:DAG:DUD:AI(D;OICI;0x1200a9;;;${VERA_SID})(A;OICI;0x1200a9;;;DU)${ROOT_ID}" "Проекты"
smb 'mkdir "Кадры\Оценка"'

TMP=$(mktemp -d)
put() { # путь на ресурсе, размер в байтах (0 — текст)
    if [ "$2" -gt 0 ]; then
        head -c "$2" /dev/zero > "$TMP/f"
    else
        printf 'Документ стенда: %s\n' "$1" > "$TMP/f"
    fi
    smb "put \"$TMP/f\" \"$1\""
}

put 'Общая\Регламент.txt' 0
put 'Общая\Секрет директора.txt' 0
put 'Общая\~$Регламент.txt' 0
put 'Общая\Thumbs.db' 0
put 'Общая\Архив.bin' 2097152
put 'Кадры\Отпуска.txt' 0
put 'Кадры\Оценка\Итоги.txt' 0
put 'Проекты\План.txt' 0
put 'Проекты\Для Веры.txt' 0

# Наследование отключено: только gleb (и администраторы, и агент).
acl 'Общая\Секрет директора.txt' --inherit remove
acl 'Общая\Секрет директора.txt' --add "ACL:$D\\gleb:ALLOWED/0/READ"
acl 'Общая\Секрет директора.txt' --add "ACL:$D\\Domain Admins:ALLOWED/0/FULL"
acl 'Общая\Секрет директора.txt' --add "ACL:$D\\kronto-readers:ALLOWED/0/READ"

# Явное разрешение vera сильнее унаследованного запрета с папки.
acl 'Проекты\Для Веры.txt' --add "ACL:$D\\vera:ALLOWED/0/READ"

rm -rf "$TMP"
echo "seed: ok"
