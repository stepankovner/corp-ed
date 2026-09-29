"""Адаптер Confluence Server / Data Center: страницы и вложения
пространств от имени служебной учётной записи (режим organization),
права — из ограничений чтения страниц и предков с раскрытием групп.

Написан **без доступа к документации Atlassian** (сетевая политика
среды), по устоявшемуся REST API Server/DC (`/rest/api/space`,
`/rest/api/content`, `.../restriction/byOperation`,
`/rest/api/group/{name}/member`): формы ответов — в тестах
(`tests/connectors/fake_confluence.py`), сверить с живым Confluence
(RISKS №37).
"""

from corp_ed.connectors.confluence.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
