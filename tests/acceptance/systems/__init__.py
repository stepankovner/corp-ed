"""Драйверы систем и разбор учётных данных."""

from functools import cache
from pathlib import Path

from corp_ed.connectors.registry import KindSpec, UserAuth, default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode
from tests.acceptance.model import Manifest, System
from tests.acceptance.systems.base import (
    Creds,
    Driver,
    env_credentials,
    merge,
    read_credentials,
)
from tests.acceptance.systems.bookstack import BookStackDriver
from tests.acceptance.systems.dokuwiki import DokuWikiDriver
from tests.acceptance.systems.mediawiki import MediaWikiDriver
from tests.acceptance.systems.nextcloud import NextcloudDriver, OwnCloudDriver
from tests.acceptance.systems.outline import OutlineDriver
from tests.acceptance.systems.seafile import SeafileDriver
from tests.acceptance.systems.webdav import WebDavDriver
from tests.acceptance.systems.wikijs import WikiJsDriver

DRIVERS: dict[str, type[Driver]] = {
    "bookstack": BookStackDriver,
    "nextcloud": NextcloudDriver,
    "owncloud": OwnCloudDriver,
    "dokuwiki": DokuWikiDriver,
    "webdav": WebDavDriver,
    "wikijs": WikiJsDriver,
    "mediawiki": MediaWikiDriver,
    "seafile": SeafileDriver,
    "outline": OutlineDriver,
}

# Поля токенов OAuth-сотрудника (то, что ядро кладёт в грант после обмена
# кода; см. OWNER-CHECKLIST.md, как их получить).
OAUTH_USER_FIELDS = ["access_token", "refresh_token", "expires_at", "user_id"]


@cache
def kind_specs() -> dict[str, KindSpec]:
    registry = default_registry(ConnectorSettings(secrets_keys="x"))  # type: ignore[arg-type,call-arg]
    return dict(registry._specs)  # noqa: SLF001 — каталог видов, в том числе скрытых


class GenericDriver(Driver):
    """Система без своего засева (облако): подключение по KindSpec,
    документы раскладывает владелец по `--plan`, сценарий проверяет."""

    def __init__(self, system: System, manifest: Manifest, creds: Creds | None) -> None:
        super().__init__(system, manifest, creds)
        spec = kind_specs()[system.kind]
        self.mode = spec.mode
        self.modules = tuple(system.modules) or (spec.modules[0].name,)
        self.config_required = any(f.required for f in spec.config_fields)


def make_driver(system: System, manifest: Manifest, creds: Creds | None) -> Driver:
    return DRIVERS.get(system.id, GenericDriver)(system, manifest, creds)


def resolve_credentials(
    system: System, manifest: Manifest, path: Path | None
) -> Creds | None:
    """Файл учётных данных, поверх него — переменные окружения ACC_…"""
    spec = kind_specs()[system.kind]
    from_file = read_credentials(path, system.id)
    user_fields = (
        OAUTH_USER_FIELDS
        if spec.user_auth is UserAuth.OAUTH
        else [f.name for f in spec.credential_fields]
        if spec.mode is ConnectorMode.PER_USER
        else []
    )
    org_fields = (
        [f.name for f in spec.credential_fields]
        if spec.mode is ConnectorMode.ORGANIZATION
        else []
    )
    from_env = env_credentials(
        system.id,
        [f.name for f in spec.config_fields],
        org_fields,
        [f.name for f in spec.app_credential_fields],
        user_fields,
        list(manifest.employees),
    )
    return merge(from_file, from_env)
