"""Адаптер Microsoft 365 (режим organization): SharePoint Online — сайты и
библиотеки документов — и OneDrive сотрудников через Microsoft Graph
v1.0, приложение Entra ID с application permissions и секретом клиента.

Написан по справочнику Microsoft Graph и Microsoft identity platform
(learn.microsoft.com, 15.10.2026) без живого клиента: подписку из России
не оформить. Формы ответов — в `tests/connectors/fake_m365.py` (там же
ссылки на страницы документации); сверить через
`cli connector-check --kind m365`. Ограничения прав — access.py.
"""

from corp_ed.connectors.m365.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
