"""Поддельный источник для тестов ядра коннекторов.

Один FakeSource на тест: документы, содержимое, какие токены отвергать
и что видно какому токену. Адаптер над ним — тот же интерфейс, что у
настоящих (connectors/base.py), без сети.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    FetchedContent,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
)
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind

FAKE_KIND = "fake_org"
FAKE_PER_USER_KIND = "fake_per_user"
PUBLIC_IP = "93.184.216.34"

_MODULES = (ModuleSpec("docs", "Документы"), ModuleSpec("wiki", "Вики"))
_CONFIG = (
    FieldSpec("base_url", "Адрес системы"),
    FieldSpec("root", "Корневая папка", required=False),
)
_CREDENTIALS = (FieldSpec("token", "Токен", secret=True),)

ORG_SPEC = KindSpec(
    kind=FAKE_KIND,
    title="Поддельная система (организация)",
    mode=ConnectorMode.ORGANIZATION,
    modules=_MODULES,
    config_fields=_CONFIG,
    credential_fields=_CREDENTIALS,
    url_field="base_url",
)
PER_USER_SPEC = KindSpec(
    kind=FAKE_PER_USER_KIND,
    title="Поддельная система (сотрудники)",
    mode=ConnectorMode.PER_USER,
    modules=_MODULES,
    config_fields=_CONFIG,
    credential_fields=_CREDENTIALS,
    url_field="base_url",
)


async def public_resolver(host: str) -> list[str]:
    return [PUBLIC_IP]


def doc(
    external_id: str,
    title: str | None = None,
    *,
    version: str = "v1",
    module: str = "docs",
    kind: RemoteDocumentKind = RemoteDocumentKind.FILE,
    visibility: MaterialVisibility = MaterialVisibility.TENANT,
    emails: Sequence[str] = (),
    filename: str | None = None,
) -> RemoteDocument:
    return RemoteDocument(
        external_id=external_id,
        title=title or external_id,
        url=f"https://portal.example.com/docs/{external_id}",
        version=version,
        kind=kind,
        module=module,
        filename=filename or f"{external_id}.txt",
        visibility=visibility,
        allowed_emails=frozenset(emails),
    )


@dataclass
class FakeSource:
    documents: list[RemoteDocument] = field(default_factory=list)
    contents: dict[str, FetchedContent] = field(default_factory=dict)
    # Токены, которые источник отвергает (AdapterAuthError при check).
    rejected_tokens: set[str] = field(default_factory=set)
    # Источник недоступен: AdapterError(retryable=True) при check.
    unavailable: bool = False
    # Токен → external_id документов, видимых этому токену; нет записи —
    # видно всё.
    visible_to: dict[str, set[str]] = field(default_factory=dict)
    # external_id → код ошибки при fetch.
    fetch_errors: dict[str, str] = field(default_factory=dict)
    fetch_calls: list[str] = field(default_factory=list)
    check_calls: list[str] = field(default_factory=list)
    listed_modules: list[list[str]] = field(default_factory=list)

    def add(self, document: RemoteDocument, text: str | None = None) -> RemoteDocument:
        self.documents.append(document)
        if text is not None:
            self.contents[document.external_id] = FetchedFile(
                data=text.encode("utf-8"), filename=document.filename or "doc.txt"
            )
        return document


class FakeAdapter:
    def __init__(self, source: FakeSource, credentials: Mapping[str, str]) -> None:
        self.source = source
        self.token = credentials.get("token", "")

    async def check(self) -> None:
        self.source.check_calls.append(self.token)
        if self.source.unavailable:
            raise AdapterError("http_503", retryable=True)
        if self.token in self.source.rejected_tokens:
            raise AdapterAuthError()

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        self.source.listed_modules.append(list(modules))
        visible = self.source.visible_to.get(self.token)
        for document in list(self.source.documents):
            if document.module not in modules:
                continue
            if visible is not None and document.external_id not in visible:
                continue
            yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        self.source.fetch_calls.append(document.external_id)
        if document.external_id in self.source.fetch_errors:
            raise AdapterError(self.source.fetch_errors[document.external_id])
        if self.token in self.source.rejected_tokens:
            raise AdapterAuthError()
        content = self.source.contents[document.external_id]
        if isinstance(content, FetchedFile) and len(content.data) > max_bytes:
            raise AdapterError("document_too_large")
        return content


def make_registry(source: FakeSource) -> AdapterRegistry:
    registry = AdapterRegistry()

    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> FakeAdapter:
        return FakeAdapter(source, credentials)

    registry.register(ORG_SPEC, factory)
    registry.register(PER_USER_SPEC, factory)
    return registry


async def plain_extractor(fmt: object, data: bytes) -> str:
    """Вместо песочницы: тесты ядра проверяют синхронизацию, а не разбор."""
    return data.decode("utf-8")
