# Фикстуры Outline

## `live/` — записи со стенда Outline 1.10.1 (09.10.2026)

Записаны `cli connector-check --kind outline --fetch 2 --record` ключом
администратора на стенде `tests/live/outline/` (Docker, вход через dex,
компания из `tests/live/outline/seed.py`): `auth.info`, `users.list`,
`collections.list`, `documents.list`, участники коллекций, групп и
документов. Каждый файл — `{"method", "params", "response"}`; ключ в
заголовке не пишется, тела прошли `redact`. Коллекция Welcome, которую
Outline создаёт сам (её документы — тексты Outline), из `collections.list`
и `documents.list` вырезана; отсутствие ключа и паролей стенда проверено
перед коммитом. Почты, имена и id — выдуманной компании стенда.

Контракт — `tests/connectors/test_outline_live_fixtures.py`: адаптер на
этих ответах даёт те же права, что Outline (так нашлось, что группы
коллекции Outline 1.10 отдаёт в `groupMemberships`).
