"""Адаптер Р7-Офис Корпоративного сервера 2019 (API ONLYOFFICE Workspace):
«Общие документы» портала с зеркалом прав по учётке администратора.

Написан по документации Р7 (support.r7-office.ru/community_server/api/,
15.10), совпадающей с API ONLYOFFICE Community Server, без живой
системы: формы ответов — в тестах (`tests/connectors/fake_r7office.py`),
сверить через `cli connector-check --kind r7office`. Ограничения прав —
в докстроке adapter.py.
"""

from corp_ed.connectors.r7office.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
