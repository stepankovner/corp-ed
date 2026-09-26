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

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from corp_ed.connectors.common import Recorder
from corp_ed.domain.types import MaterialVisibility, RemoteDocumentKind


@dataclass(frozen=True)
class AdapterOptions:
    """Что фабрика адаптера получает сверх config и credentials.

    recorder — записывать ответы источника без секретов (фикстуры для
    контрактных тестов); fast — без пауз между запросами (только
    диагностика: коробочные системы и тесты). В синхронизации — по
    умолчанию: пусто и с паузами.
    """

    recorder: Recorder | None = None
    fast: bool = False


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


class AdapterConfigError(AdapterError):
    """Подключение настроено неверно на уровне компании: секрет приложения
    отвергнут сервером авторизации, у приложения или вебхука нет нужного
    scope, приложение удалено с портала. Останавливает коннектор целиком
    в любом режиме; гранты сотрудников не трогаются — их токены ни при
    чём, и после исправления настроек они продолжат работать."""

    def __init__(self, code: str) -> None:
        super().__init__(code, retryable=False)


@dataclass(frozen=True)
class RemoteDocument:
    """Документ, каким его видит источник.

    external_id — стабильный идентификатор в системе; version — что
    угодно, что меняется при изменении документа (etag, дата, ревизия):
    по нему ядро решает, скачивать ли заново. path — путь/крошки в
    источнике для человека; locator — адрес документа для fetch на
    момент листинга (путь на Диске, если id по API не адресуется),
    ядро его не хранит. visibility и allowed_emails заполняет адаптер
    режима organization; в режиме per_user ядро само считает документ
    ограниченным листингом.
    """

    external_id: str
    title: str
    url: str
    version: str
    kind: RemoteDocumentKind
    module: str
    path: str = ""
    locator: str = ""
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


@dataclass(frozen=True)
class FetchedMarkdown:
    """Документ, который источник отдаёт уже в Markdown (База знаний 2.0
    Битрикс24, Яндекс Вики): в конвейер идёт как есть, без очистки HTML."""

    markdown: str


FetchedContent = FetchedFile | FetchedPage | FetchedMarkdown


@dataclass(frozen=True)
class ExchangedCredentials:
    """Результат OAuth-обмена: учётные данные сотрудника для SecretBox и
    его идентификатор в источнике, если сервер авторизации его отдал."""

    credentials: Mapping[str, str]
    external_user_id: str | None = None


class OAuthFlow(Protocol):
    """OAuth-обмен режима per_user для одного подключения.

    Собирается из config и учётных данных приложения (client_secret),
    как адаптер — из учётных данных сотрудника. Сам state подписывает и
    проверяет ядро: адаптер получает его готовым.
    """

    def authorize_url(self, state: str) -> str:
        """Адрес, куда отправить браузер сотрудника."""
        ...

    async def exchange(self, code: str) -> ExchangedCredentials:
        """Обменять одноразовый код на учётные данные.

        AdapterAuthError — код не принят (истёк, чужой); AdapterConfigError
        — не принято само приложение (client_id/secret, не установлено,
        не оплачено); AdapterError — сервер авторизации недоступен.
        """
        ...


def refreshed_credentials(adapter: object) -> Mapping[str, str] | None:
    """Новые учётные данные, если адаптер обновил токены по ходу работы.

    Битрикс24 выдаёт новую пару access/refresh при каждом продлении, и
    старый refresh перестаёт действовать: не сохранить новую — потерять
    авторизацию сотрудника. Адаптер держит их в атрибуте
    refreshed_credentials (None — ничего не менялось); ядро после
    check/list/fetch перешифровывает и записывает в грант или подключение.
    """
    value = getattr(adapter, "refreshed_credentials", None)
    return value if isinstance(value, Mapping) else None


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
