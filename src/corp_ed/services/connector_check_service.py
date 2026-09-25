"""Проверка адаптера против настоящего источника без базы: cli connector-check.

Что делает: check → листинг выбранных модулей (до limit документов) →
скачивание первых N и прогон через тот же конвейер, что у
синхронизации (detect_format + песочница или html_to_markdown). Ничего
не пишет: ни материалов, ни грантов. Нужна команде для приёмки этапа
(«документ виден и читается») и для записи контрактных фикстур.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from corp_ed.connectors.base import (
    AdapterError,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
    SourceAdapter,
)
from corp_ed.connectors.html import html_to_markdown
from corp_ed.core.outbound import OutboundURLError
from corp_ed.ingest.extract import ExtractionError, SourceFormat, detect_format
from corp_ed.ingest.sandbox import extract_isolated

Extractor = Callable[[SourceFormat, bytes], Awaitable[str]]
CHECK_TIMEOUT = 20.0


@dataclass(frozen=True)
class FetchReport:
    external_id: str
    title: str
    format: str | None = None
    size: int | None = None
    chars: int | None = None
    sha256: str | None = None
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


@dataclass
class CheckReport:
    check_ok: bool
    check_code: str | None = None
    documents: list[RemoteDocument] = field(default_factory=list)
    truncated: bool = False
    fetched: list[FetchReport] = field(default_factory=list)
    list_error_code: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.check_ok
            and self.list_error_code is None
            and all(report.ok for report in self.fetched)
        )


async def run_check(
    adapter: SourceAdapter,
    *,
    modules: Sequence[str],
    limit: int,
    fetch: int,
    max_bytes: int,
    extractor: Extractor = extract_isolated,
) -> CheckReport:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            await adapter.check()
    except (AdapterError, OutboundURLError) as exc:
        return CheckReport(False, getattr(exc, "code", "source_unavailable"))
    except TimeoutError:
        return CheckReport(False, "timeout")
    report = CheckReport(True)
    try:
        async for document in adapter.list(list(modules)):
            if len(report.documents) >= limit:
                report.truncated = True
                break
            report.documents.append(document)
    except (AdapterError, OutboundURLError) as exc:
        report.list_error_code = getattr(exc, "code", "source_unavailable")
    # По одному документу каждого модуля, чтобы увидеть и диск, и базу знаний.
    to_fetch: list[RemoteDocument] = []
    seen_modules: dict[str, int] = {}
    for document in report.documents:
        if seen_modules.get(document.module, 0) < fetch:
            seen_modules[document.module] = seen_modules.get(document.module, 0) + 1
            to_fetch.append(document)
    for document in to_fetch:
        report.fetched.append(
            await _fetch(adapter, document, max_bytes=max_bytes, extractor=extractor)
        )
    return report


async def _fetch(
    adapter: SourceAdapter,
    document: RemoteDocument,
    *,
    max_bytes: int,
    extractor: Extractor,
) -> FetchReport:
    try:
        content = await adapter.fetch(document, max_bytes=max_bytes)
        if isinstance(content, FetchedFile):
            detected = detect_format(content.filename, content.data)
            markdown = await extractor(detected.format, content.data)
            return FetchReport(
                document.external_id,
                document.title,
                format=detected.format.value,
                size=len(content.data),
                chars=len(markdown),
                sha256=hashlib.sha256(content.data).hexdigest(),
            )
        if isinstance(content, FetchedPage):
            markdown = html_to_markdown(content.html)
            return FetchReport(
                document.external_id,
                document.title,
                format="html",
                size=len(content.html.encode("utf-8")),
                chars=len(markdown),
                sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            )
        return FetchReport(
            document.external_id, document.title, error_code="unsupported_format"
        )
    except (AdapterError, ExtractionError, OutboundURLError) as exc:
        return FetchReport(
            document.external_id,
            document.title,
            error_code=getattr(exc, "code", "source_unavailable"),
        )
