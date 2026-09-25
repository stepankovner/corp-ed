# Фикстуры REST Битрикс24

Образцы ответов **из официальной документации** (зеркало
`bitrix-tools/b24-rest-docs`, страницы методов, состояние на 25.09.2026):
тестовый портал из этой среды недоступен (сетевая политика), записать
живые ответы было нельзя. Каждый файл — `{"method", "params", "response"}`,
как пишет `cli connector-check --record DIR`; токены в образцах заменены.

Когда портал станет доступен: `python -m corp_ed.cli connector-check
--kind bitrix24 --config portal=… --credential webhook=… --record
tests/connectors/fixtures/bitrix24/live` и сравнить формы ответов с этими.

## `live/` — записи с тестового портала (26.09.2026)

Записаны `cli connector-check --record` по входящему вебхуку (права
`disk`, `landing`, `user`) через egress-прокси среды
(`CONNECTOR_OUTBOUND_VIA_PROXY=true`): `profile`, `disk.storage.getlist`,
`disk.storage.getchildren` (личный диск с двумя PDF и пустой «Общий
диск»), `landing.site.getlist` (пустые списки — базы знаний на портале
нет), `note.collection.list` (ошибка REST 3.0 без scope `note`),
`disk.file.get`. Формы совпали с образцами документации выше. Секреты
вырезаны `redact`: параметр `token` в `DOWNLOAD_URL` и код вебхука в
пути `/rest/{user}/{code}/`; отсутствие значений переменных `BITRIX24_TEST_*`
в файлах проверено скриптом перед коммитом. Записи базы знаний,
`note.document.*` и OAuth-обмена появятся, когда на портале будут база
знаний, scope `note` и грант сотрудника (RISKS №32).
