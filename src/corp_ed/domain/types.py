from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True)
class ChunkMatch:
    """Найденный чанк в том виде, в каком его видит сервис ответа.

    content — llm_text чанка (крошки + Markdown), именно он уходит в
    промпт. title и heading_path нужны промпту v2, чтобы подписать
    выдержку источником «[1] Документ > Раздел», и фронту — чтобы
    показать ссылку. Поля совпадают с Protocol SourceChunk из
    prompts/faq.py: наследоваться от него не нужно.
    """

    id: UUID
    content: str
    material_id: UUID
    position: int
    distance: float
    title: str
    heading_path: list[str]
    fulltext_rank: float | None = None
    """ts_rank_cd по полнотекстовой ветке; None — чанк найден только
    вектором. distance известен всегда: полнотекстовая ветка считает его
    тем же запросом."""
    source_url: str | None = None
    """Ссылка на документ в источнике (коннекторы); у ручных загрузок
    пусто. Фронт показывает её в источниках ответа (досье 12.2)."""


class AnswerOrigin(StrEnum):
    """Откуда взят ответ.

    DOCUMENTS — из выдержек документов компании, со ссылками.
    GENERAL_KNOWLEDGE — в документах ответа не нашлось, модель ответила
    из общих знаний. Такой ответ всегда помечен: первой строкой текста
    (GENERAL_ANSWER_PREFIX) и этим полем — фронт показывает предупреждение
    по полю, не разбирая текст.
    NONE — в документах ответа нет, а компания выбрала строгий режим:
    честный отказ NOT_FOUND_ANSWER без ответа из общих знаний.
    """

    DOCUMENTS = "documents"
    GENERAL_KNOWLEDGE = "general_knowledge"
    NONE = "none"


class Retriever(StrEnum):
    """Как искать выдержки (M1, BH-12).

    VECTOR — только векторный поиск, порог на каждой выдержке.
    HYBRID — вектор + полнотекст, слияние RRF; порог — на лучшем
    векторном кандидате (скор RRF зависит только от рангов).
    """

    VECTOR = "vector"
    HYBRID = "hybrid"


class NotFoundMode(StrEnum):
    """Что делать, когда в документах ответа нет (Р1, BH-24).

    GENERAL — ответ из общих знаний со строгой пометкой (решение 25.09,
    по умолчанию). STRICT — честный отказ, как в досье v3.2: для
    компаний, которым нельзя ничего сверх их документов.
    """

    GENERAL = "general"
    STRICT = "strict"


@dataclass(frozen=True)
class AnswerDiagnostics:
    """Сведения для админа и eval (E5 — стоимость и модель ответа).

    model пуст, если модель не вызывалась (строгий отказ без выдержек).
    model_version — что провайдер вернул в ответе: /latest переключается
    на новую версию без предупреждения, по журналу это должно быть видно.
    """

    model: str | None
    model_version: str | None
    prompt_version: str
    input_tokens: int
    output_tokens: int
    credits: int
    nearest_distance: float | None


@dataclass(frozen=True)
class FaqAnswer:
    content: str
    answer_given: bool
    """Ответ дан ПО ДОКУМЕНТАМ. Для общего ответа — False: для журнала
    ответов и отчёта о пробелах это вопрос, на который в базе знаний
    ответа нет."""
    origin: AnswerOrigin
    sources: list[ChunkMatch]
    log_id: UUID | None = None
    """Запись qa_log — к ней сотрудник ставит 👍/👎."""
    diagnostics: AnswerDiagnostics | None = None


class GapStatus(StrEnum):
    """Что админ сделал с пробелом в отчёте (BH-22)."""

    NEW = "new"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ConnectorMode(StrEnum):
    """Как коннектор получает документы и права (DECISIONS «Коннекторы»).

    ORGANIZATION — админ подключает одну учётную запись на компанию,
    адаптер выгружает документы и их ACL (Confluence). PER_USER — админ
    ставит приложение, каждый сотрудник авторизует его сам; документ
    виден сотруднику, если есть в ЕГО листинге (Битрикс24, Яндекс).
    """

    ORGANIZATION = "organization"
    PER_USER = "per_user"


class ConnectorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    # Остановлен системой: учётные данные не работают. До вмешательства
    # админа не синхронизируется.
    ERROR = "error"


class GrantStatus(StrEnum):
    """Состояние авторизации сотрудника в коннекторе per_user."""

    ACTIVE = "active"
    # Источник отверг токен: сотруднику нужно подключиться заново.
    EXPIRED = "expired"
    REVOKED = "revoked"


class SyncRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    # Часть документов пропущена (ошибка или бюджет запуска исчерпан).
    PARTIAL = "partial"
    FAILED = "failed"


class SyncTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class MaterialVisibility(StrEnum):
    """Кто из сотрудников компании видит документ в поиске.

    TENANT — все (ручные загрузки и документы без ограничений в
    источнике). RESTRICTED — только те, у кого есть строка в
    material_access: права источника, зеркалированные коннектором.
    """

    TENANT = "tenant"
    RESTRICTED = "restricted"


class RemoteDocumentKind(StrEnum):
    FILE = "file"
    PAGE = "page"
