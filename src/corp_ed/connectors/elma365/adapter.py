"""Спецификация вида elma365 и адаптер (режим per_user, токен сотрудника).

Токен ELMA365 привязан к пользователю: «укажите пользователя, от имени
которого будут производиться все действия под добавленным токеном»,
«внешнему приложению будут видны только те записи, к которым у
выбранного пользователя есть доступ» (elma365.com/ru/help/platform/
token.html). Выпускают токены только администраторы ELMA365
(«Администрирование → Токены»): администратор выпускает токен на каждого
сотрудника и передаёт ему, сотрудник вставляет свой в «Мои подключения».

Почему per_user, а не зеркало прав: в Web API нет прав файла или папки
со списком пользователей — только 403 на запрос одного объекта. Есть
схема прав приложения (`ApplicationEntityPermissions`), но не раздела
«Файлы». Ограничение: kronto не может проверить, что токен выпущен
именно на этого сотрудника, — если администратор раздаст всем токен
одного пользователя, все увидят его файлы.

Модуль один — files, раздел «Файлы» (обход и проверка прав — files.py).
Вложения элементов приложений не берутся: для них нужны коды разделов и
приложений и поля с файлами каждого приложения.
"""

import re
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
from corp_ed.connectors.elma365 import files as files_module
from corp_ed.connectors.elma365.client import Elma365Client
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

KIND = "elma365"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _folders(config: Mapping[str, str]) -> list[str]:
    return [
        f.strip().lower() for f in config.get("folders", "").split(",") if f.strip()
    ]


def _check_config(config: Mapping[str, str]) -> str | None:
    if any(not _UUID.match(folder) for folder in _folders(config)):
        return "folders_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="ELMA365 (раздел «Файлы»)",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.FIELDS,
    modules=(
        ModuleSpec(
            files_module.MODULE_FILES,
            "Файлы (docx, doc, xlsx, pptx, pdf, txt, md)",
        ),
    ),
    config_fields=(
        FieldSpec("base_url", "Адрес компании ELMA365 (https://компания.elma365.ru)"),
        FieldSpec(
            "folders",
            "ID папок раздела «Файлы» через запятую (пусто — все доступные)",
            required=False,
        ),
    ),
    credential_fields=(
        FieldSpec(
            "token",
            "Ваш токен ELMA365 (выпускает администратор: Администрирование → Токены)",
            secret=True,
        ),
    ),
    url_field="base_url",
    config_check=_check_config,
    base=False,
    preview=True,
)


class Elma365Adapter:
    def __init__(self, client: Elma365Client, *, folders: Sequence[str] = ()) -> None:
        self._client = client
        self._folders = tuple(folders)

    def _files(self) -> files_module.FilesModule:
        return files_module.FilesModule(self._client, folders=self._folders)

    async def check(self) -> None:
        # Метода «кто я» в Web API нет: проверка — список папок из одной.
        await self._files().check()

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if files_module.MODULE_FILES in set(modules):
            async for document in self._files().walk():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(files_module.PREFIX):
            return await self._files().fetch(document, max_bytes=max_bytes)
        raise AdapterError("unknown_document")


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    *,
    recorder: Recorder | None = None,
) -> Elma365Adapter:
    base_url = config.get("base_url", "").strip()
    if not base_url:
        raise AdapterConfigError("base_url_missing")
    token = credentials.get("token", "").strip()
    if not token:
        raise AdapterAuthError("credentials_missing")
    if _check_config(config):
        raise AdapterConfigError("folders_invalid")
    return Elma365Adapter(
        Elma365Client(http, base_url=base_url, token=token, recorder=recorder),
        folders=_folders(config),
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> Elma365Adapter:
        return build_adapter(config, credentials, http, recorder=options.recorder)

    registry.register(SPEC, factory)
