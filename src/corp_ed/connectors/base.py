"""Интерфейс адаптера источника и его типы.

Адаптер знает протокол одной системы и ничего больше: ни базы, ни
компании, ни того, кто из сотрудников что видит. Он отдаёт документы
(list), содержимое (fetch) и умеет проверить учётные данные (check).
Права он описывает словами источника: «без ограничений» или список
почт; сопоставление с нашими пользователями — в ядре.

Всё, что адаптер получил из системы клиента, — враждебный ввод: HTML
страниц чистится, файлы идут через те же проверки и песочницу, что и
ручная загрузка.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from corp_ed.domain.types import MaterialVisibility, RemoteDocumentKind


class AdapterError(Exception):
    """Сбой источника. code — для журнала и админа; retryable — повторить
    ли задачу (сеть, 5xx, лимит API) или это окончательно."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AdapterAuthError(AdapterError):
    """Учётные данные не приняты: коннектор (или грант сотрудника)
    останавливается до вмешательства человека."""

    def __init__(self, code: str = "auth_failed") -> None:
        super().__init__(code, retryable=False)


@dataclass(frozen=True)
class RemoteDocument:
    """Документ, каким его видит источник.

    external_id — стабильный идентификатор в системе; version — что
    угодно, что меняется при изменении документа (etag, дата, ревизия):
    по нему ядро решает, скачивать ли заново. path — путь/крошки в
    источнике (для будущего вывода прав из папок). visibility и
    allowed_emails заполняет адаптер режима organization; в режиме
    per_user ядро само считает документ ограниченным листингом.
    """

    external_id: str
    title: str
    url: str
    version: str
    kind: RemoteDocumentKind
    module: str
    path: str = ""
    filename: str | None = None
    size: int | None = None
    modified_at: datetime | None = None
    visibility: MaterialVisibility = MaterialVisibility.RESTRICTED
    allowed_emails: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class FetchedFile:
    """Файл байтами: идёт в detect_format и песочницу, как загрузка."""

    data: bytes
    filename: str


@dataclass(frozen=True)
class FetchedPage:
    """Страница (вики, база знаний): HTML, который ядро чистит и
    переводит в Markdown. Скрипты, формы и внешние загрузки отбрасываются."""

    html: str


FetchedContent = FetchedFile | FetchedPage


class SourceAdapter(Protocol):
    async def check(self) -> None:
        """Проверить учётные данные без загрузки документов.

        AdapterAuthError — не приняты; AdapterError — источник недоступен.
        """
        ...

    def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        """Постраничный обход документов выбранных модулей.

        Неподдерживаемые форматы и слишком большие файлы адаптер может
        отдавать — ядро их пропустит и посчитает; может и не отдавать.
        """
        ...

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        """Скачать документ. Больше max_bytes — AdapterError('document_too_large')."""
        ...
