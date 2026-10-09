"""Спецификация вида kaiten и адаптер (режим per_user, личный токен).

OAuth у Kaiten нет — только Bearer-токен из профиля сотрудника
(`{сайт}/profile/api-key`). Админ заводит подключение с адресом сайта
Kaiten (облако — `https://<компания>.kaiten.ru`, коробка — свой домен);
каждый сотрудник вставляет свой токен и видит в ответах ровно то, что
API отдаёт по его токену.

Почему per_user, а не зеркало прав: у документа есть `access`
(`for_everyone` или `by_invite`), но метода «кто приглашён в документ»
в API нет (оглавление developers.kaiten.ru, 09.10) — права документа
`by_invite` не восстановить. Видимость же у каждого сотрудника своя и
надёжная: дерево документов отдаёт только читаемое им.

Модули: documents — раздел «Документы» (ProseMirror JSON → Markdown);
card_files — вложения карточек поддерживаемых форматов. Лимит API —
50 запросов в секунду, 429 — пауза до сброса (client.py).
"""

from collections.abc import AsyncIterator, Mapping, Sequence

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    RemoteDocument,
)
from corp_ed.connectors.common import Recorder
from corp_ed.connectors.kaiten import card_files as card_files_module
from corp_ed.connectors.kaiten import documents as documents_module
from corp_ed.connectors.kaiten.client import KaitenClient
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
    UserAuth,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode

KIND = "kaiten"


def _check_config(config: Mapping[str, str]) -> str | None:
    spaces = [s.strip() for s in config.get("spaces", "").split(",") if s.strip()]
    if any(not space.isdigit() for space in spaces):
        return "spaces_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Kaiten (документы и вложения карточек)",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.FIELDS,
    modules=(
        ModuleSpec(documents_module.MODULE_DOCUMENTS, "Документы"),
        ModuleSpec(
            card_files_module.MODULE_CARD_FILES,
            "Вложения карточек (docx, doc, xlsx, pptx, pdf, txt, md)",
        ),
    ),
    config_fields=(
        FieldSpec("base_url", "Адрес Kaiten (https://компания.kaiten.ru)"),
        FieldSpec(
            "spaces",
            "ID пространств для вложений карточек через запятую (пусто — все)",
            required=False,
        ),
    ),
    credential_fields=(
        FieldSpec("token", "Ваш API-токен Kaiten (профиль → API-ключ)", secret=True),
    ),
    url_field="base_url",
    config_check=_check_config,
    extra={"token_page": "/profile/api-key"},
    preview=True,
)


class KaitenAdapter:
    def __init__(
        self,
        client: KaitenClient,
        *,
        max_bytes: int,
        spaces: Sequence[str] = (),
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._spaces = tuple(spaces)
        self._user_id: str | None = None

    @property
    def external_user_id(self) -> str | None:
        return self._user_id

    async def check(self) -> None:
        user = await self._client.get_object("users/current")
        if user.get("id") is None:
            raise AdapterAuthError("user_unknown")
        self._user_id = str(user["id"])

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        wanted = set(modules)
        if documents_module.MODULE_DOCUMENTS in wanted:
            async for document in documents_module.DocumentsModule(self._client).walk():
                yield document
        if card_files_module.MODULE_CARD_FILES in wanted:
            files = card_files_module.CardFilesModule(
                self._client, max_bytes=self._max_bytes, spaces=self._spaces
            )
            async for document in files.walk():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(documents_module.PREFIX):
            return await documents_module.DocumentsModule(self._client).fetch(
                document, max_bytes=max_bytes
            )
        if document.external_id.startswith(card_files_module.PREFIX):
            files = card_files_module.CardFilesModule(self._client, max_bytes=max_bytes)
            return await files.fetch(document, max_bytes=max_bytes)
        raise AdapterError("unknown_document")


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> KaitenAdapter:
    base_url = config.get("base_url", "").strip()
    if not base_url:
        raise AdapterConfigError("base_url_missing")
    token = credentials.get("token", "").strip()
    if not token:
        raise AdapterAuthError("credentials_missing")
    if _check_config(config):
        raise AdapterConfigError("spaces_invalid")
    return KaitenAdapter(
        KaitenClient(http, base_url=base_url, token=token, recorder=recorder),
        max_bytes=settings.max_document_bytes,
        spaces=config.get("spaces", "").split(","),
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> KaitenAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
