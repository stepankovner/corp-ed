"""Семейство WebDAV-дисков: Nextcloud (пароль приложения или OAuth2),
ownCloud, Seafile (SeafDAV), Диск VK WorkSpace, Облако Mail.ru и просто
WebDAV (NAS). Режим per_user для всех — почему, в kinds.py.

Состав: multistatus — разбор ответа PROPFIND; client — запросы, коды
ошибок, ожидание по Retry-After; oauth — OAuth2 Nextcloud; kinds —
формы видов и их отличия; adapter — обход дерева и сборка.
Регистрируется в default_registry().
"""

from collections.abc import Mapping

from corp_ed.connectors.base import AdapterConfigError, AdapterOptions
from corp_ed.connectors.registry import AdapterRegistry, KindSpec
from corp_ed.connectors.webdav.adapter import WebDavAdapter, build_adapter
from corp_ed.connectors.webdav.kinds import NEXTCLOUD_OAUTH, SPECS
from corp_ed.connectors.webdav.oauth import NextcloudOAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient

__all__ = ["SPECS", "register"]


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> WebDavAdapter:
        return build_adapter(
            spec.kind, config, credentials, http, settings, recorder=options.recorder
        )

    def oauth(
        spec: KindSpec,
        config: Mapping[str, str],
        app_credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> NextcloudOAuth:
        server = config.get("server", "")
        client_id = config.get("client_id", "")
        client_secret = app_credentials.get("client_secret", "")
        if not server or not client_id or not client_secret:
            raise AdapterConfigError("app_credentials_missing")
        return NextcloudOAuth(
            http, server=server, client_id=client_id, client_secret=client_secret
        )

    for spec in SPECS:
        registry.register(spec, factory, oauth if spec is NEXTCLOUD_OAUTH else None)
