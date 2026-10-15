#!/bin/bash
# Наполнение вики стенда (только на чистой вики): три сотрудника, страницы
# в разных пространствах имён, перенаправление, файлы и бот-пароли.
#
#   docker compose -f tests/live/mediawiki/compose.yaml exec wiki bash /stand/seed.sh
#   docker compose -f tests/live/mediawiki/compose.yaml exec wiki_private bash /stand/seed.sh
#
# Бот-пароли (ровно 32 символа, выдуманные):
#   Admin@kronto — то, что вводит админ компании в kronto (grants: basic);
#   Admin@seed   — правки, удаление и блокировки из живых тестов;
#   Petr@kronto  — учётка без права чтения на закрытой вики.
set -euo pipefail
cd /var/www/html
run() { php maintenance/run.php "$@"; }

run createAndPromote --force Ivan Kronto-Test-1-ivan --custom-groups staff
run createAndPromote --force Maria Kronto-Test-1-maria --custom-groups staff,hr
run createAndPromote --force Petr Kronto-Test-1-petr

page() {
    # page <заголовок> <текст>
    printf '%s' "$2" | run edit --user Admin --summary seed "$1" > /dev/null
}

page "Шаблон:Подпись" "''Отдел кадров, телефон 100.''"
page "Отпуск" "== Отпуск ==
Ежегодный отпуск — '''28 дней'''. Заявление — за две недели.
* Перенос — по согласованию с руководителем.
{{Подпись}}"
page "Больничный" "Больничный лист оформляется электронно; справку сообщить в течение трёх дней."
page "Старое название" "#REDIRECT [[Отпуск]]"
page "Компания:Правила" "Правила работы с вики: писать коротко, ссылаться на источники."
page "Справка:Поиск" "Поиск по вики — строка вверху страницы."
page "Участник:Ivan" "Личная страница Ивана: черновики."
page "Кадры:Зарплаты" "Зарплаты выплачиваются 5 и 20 числа."

mkdir -p /tmp/seed-files
printf 'Регламент закупок: заявка, согласование, оплата.\n' > /tmp/seed-files/Регламент.txt
# Картинка 1×1 — формат, который ассистент не читает: пропускается.
printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82' > /tmp/seed-files/Схема.png
run importImages --user Admin --comment seed /tmp/seed-files > /dev/null

run createBotPassword --appid kronto --grants basic Admin kronto0stand0admin0bot0pass00001
run createBotPassword --appid seed --grants basic,editpage,createeditmovepage,delete,blockusers Admin kronto0stand0seed00bot0pass00001
run createBotPassword --appid kronto --grants basic Petr kronto0stand0petr00bot0pass00001
echo "seed: ok"
