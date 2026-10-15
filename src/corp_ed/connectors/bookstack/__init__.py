"""Адаптер BookStack (вики своей установки): страницы книг и вложения с
зеркалом права «просмотр» по API-токену администратора.

Написан по встроенной документации API (`/api/docs`) и коду BookStack
v26.09 (права — app/Permissions, permissions.py) и проверен на стенде
в Docker (`tests/live/bookstack`, linuxserver/bookstack v26.09.1): формы
ответов — в тестах (`tests/connectors/fake_bookstack.py`).

Ограничения прав — в докстроке adapter.py: «вся компания» только при
роли, в которой состоят все пользователи; неоднозначное — только роли
Admin; без права читать права содержимого подключение останавливается.
"""

from corp_ed.connectors.bookstack.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
