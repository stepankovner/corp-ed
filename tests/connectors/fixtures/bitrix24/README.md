# Фикстуры REST Битрикс24

Образцы ответов **из официальной документации** (зеркало
`bitrix-tools/b24-rest-docs`, страницы методов, состояние на 25.09.2026):
тестовый портал из этой среды недоступен (сетевая политика), записать
живые ответы было нельзя. Каждый файл — `{"method", "params", "response"}`,
как пишет `cli connector-check --record DIR`; токены в образцах заменены.

Когда портал станет доступен: `python -m corp_ed.cli connector-check
--kind bitrix24 --config portal=… --credential webhook=… --record
tests/connectors/fixtures/bitrix24/live` и сравнить формы ответов с этими.
