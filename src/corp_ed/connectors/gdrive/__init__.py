"""Адаптер Google Drive для компаний на Google Workspace (режим
organization): сервисный аккаунт клиента с делегированием на домен,
общие диски и диски сотрудников, права из разрешений Drive с
раскрытием групп Directory API.

Сверен со справочником Drive API v3 и Admin SDK Directory API
(developers.google.com, 09.10.2026); живым Workspace не проверен —
формы ответов в тестах (`tests/connectors/fake_google.py`).
"""

from corp_ed.connectors.gdrive.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
