"""Модуль базы знаний: сайты типа KNOWLEDGE (и GROUP — базы знаний
рабочих групп, где портал их поддерживает) → страницы → блоки.

Классическая база знаний Битрикс24 построена на «Сайтах»: страница —
набор блоков, текст лежит в HTML блока. Путь: landing.site.getlist
(верхнеуровневый scope KNOWLEDGE/GROUP — внутренний скоуп лендингов, не
REST-scope) → landing.landing.getlist по SITE_ID (только опубликованные,
не папки) → при скачивании landing.block.getlist с get_content —
опубликованный HTML активных блоков по порядку. HTML чистится ядром
(connectors/html.py).

Права: методы отдают только сайты с правом «просмотр» у пользователя
токена — на уровне сайта, не страницы. Версия страницы — DATE_MODIFY и
DATE_PUBLIC строками портала: правка блока без публикации страницу не
меняет, а после публикации меняется DATE_PUBLIC.

«База знаний 2.0» (методы note.*, REST 3.0, Markdown напрямую, scope
note) — отдельный модуль на следующем шаге: у тестового вебхука нет
scope note.
"""

from collections.abc import AsyncIterator
from html import escape
from typing import Any

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.bitrix24.client import Bitrix24Client
from corp_ed.domain.types import RemoteDocumentKind

logger = structlog.get_logger()

MODULE_KNOWLEDGE_BASE = "knowledge_base"
PREFIX = "kb:"
SCOPES = ("KNOWLEDGE", "GROUP")
PAGE_LIMIT = 50
_SITE_SELECT = ["ID", "TITLE", "CODE", "ACTIVE", "DATE_MODIFY"]
_PAGE_SELECT = [
    "ID",
    "TITLE",
    "CODE",
    "SITE_ID",
    "FOLDER",
    "DATE_MODIFY",
    "DATE_PUBLIC",
]


class KnowledgeBaseModule:
    def __init__(self, client: Bitrix24Client) -> None:
        self._client = client

    async def walk(self) -> AsyncIterator[RemoteDocument]:
        for scope in SCOPES:
            try:
                sites = await self._sites(scope)
            except AdapterError as exc:
                if (
                    scope == "GROUP"
                    and not exc.retryable
                    and not isinstance(exc, AdapterAuthError | AdapterConfigError)
                ):
                    # Порталы без баз знаний групп отвечают ошибкой метода.
                    logger.info(
                        "bitrix24_kb_scope_unavailable", scope=scope, code=exc.code
                    )
                    continue
                raise
            for site in sites:
                site_id = str(site.get("ID"))
                site_title = str(site.get("TITLE") or f"site-{site_id}")
                site_code = str(site.get("CODE") or "").strip("/")
                async for page in self._pages(scope, site_id):
                    page_id = str(page.get("ID"))
                    code = str(page.get("CODE") or "").strip("/")
                    url = page.get("PUBLIC_URL")
                    modified = page.get("DATE_MODIFY") or ""
                    published = page.get("DATE_PUBLIC") or ""
                    if not isinstance(url, str) or not url:
                        url = _page_url(self._client.portal, site_code, code)
                    yield RemoteDocument(
                        external_id=f"{PREFIX}{scope}:{page_id}",
                        title=str(page.get("TITLE") or site_title),
                        url=url,
                        version=f"{modified}|{published}",
                        kind=RemoteDocumentKind.PAGE,
                        module=MODULE_KNOWLEDGE_BASE,
                        path=f"{site_title}",
                    )

    async def fetch(self, document: RemoteDocument) -> FetchedPage:
        _, scope, page_id = document.external_id.split(":", 2)
        response = await self._client.call(
            "landing.block.getlist",
            {"scope": scope, "lid": int(page_id), "params": {"get_content": True}},
        )
        blocks = response.get("result")
        parts: list[str] = [f"<h1>{escape(document.title)}</h1>"]
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or block.get("active") is False:
                    continue
                content = block.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content)
        if len(parts) == 1:
            raise AdapterError("empty_page")
        return FetchedPage(html="\n".join(parts))

    async def _sites(self, scope: str) -> list[dict[str, Any]]:
        response = await self._client.call(
            "landing.site.getlist",
            {
                "scope": scope,
                "params": {
                    "select": _SITE_SELECT,
                    "filter": {"TYPE": scope, "ACTIVE": "Y", "DELETED": "N"},
                },
            },
        )
        result = response.get("result")
        return (
            [s for s in result if isinstance(s, dict)]
            if isinstance(result, list)
            else []
        )

    async def _pages(self, scope: str, site_id: str) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        while True:
            response = await self._client.call(
                "landing.landing.getlist",
                {
                    "scope": scope,
                    "params": {
                        "select": _PAGE_SELECT,
                        "filter": {
                            "SITE_ID": site_id,
                            "ACTIVE": "Y",
                            "DELETED": "N",
                            "FOLDER": "N",
                        },
                        "order": {"ID": "ASC"},
                        "limit": PAGE_LIMIT,
                        "offset": offset,
                        "get_urls": True,
                    },
                },
            )
            result = response.get("result")
            pages = (
                [p for p in result if isinstance(p, dict)]
                if isinstance(result, list)
                else []
            )
            for page in pages:
                yield page
            if len(pages) < PAGE_LIMIT:
                return
            offset += PAGE_LIMIT


def _page_url(portal: str, site_code: str, page_code: str) -> str:
    """Публичный адрес страницы, если getlist его не отдал: /knowledge/…"""
    parts = [p for p in (site_code, page_code) if p]
    return f"{portal}knowledge/{'/'.join(parts)}/" if parts else f"{portal}knowledge/"
