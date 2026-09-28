"""Подключение Битрикс24 к тестовому порталу для живых тестов.

Вебхук кладётся в грант сотрудника напрямую — обход OAuth только для
стенда: адаптер принимает вебхук наравне с токенами (build_client), а
через API такой грант не создать.
"""

import os
from datetime import UTC, datetime

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient, validate_outbound_url
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Connector, ConnectorUserGrant, User
from corp_ed.domain.types import ConnectorMode
from corp_ed.services.connector_sync_service import ConnectorSyncService

PORTAL = os.environ.get("BITRIX24_TEST_PORTAL", "")
WEBHOOK = os.environ.get("BITRIX24_TEST_WEBHOOK", "")
HAVE_PORTAL = bool(PORTAL and WEBHOOK)
# За egress-прокси (HTTPS_PROXY) запросы идут по имени — как в бою с
# CONNECTOR_OUTBOUND_VIA_PROXY; явный флаг сильнее автоопределения.
_FLAG = os.environ.get("CONNECTOR_OUTBOUND_VIA_PROXY", "").lower()
VIA_PROXY = _FLAG in {"1", "true", "yes"} or (
    not _FLAG and bool(os.environ.get("HTTPS_PROXY"))
)


class Bitrix24Stand:
    def __init__(self) -> None:
        key = Fernet.generate_key().decode()
        self.secrets = SecretBox([key])
        self.settings = ConnectorSettings(  # type: ignore[call-arg]
            secrets_keys=key,  # type: ignore[arg-type]
            outbound_via_proxy=VIA_PROXY,
        )
        self.portal = ""

    async def connect(self, session: AsyncSession, employee: User) -> Connector:
        """Подключение per_user с личным и общим диском и грант сотрудника.

        Без модулей базы знаний: у вебхука стенда нет scope note, а
        landing на пустом портале ничего не даёт.
        """
        self.portal = (await validate_outbound_url(PORTAL)).url
        connector = Connector(
            kind="bitrix24",
            name="Битрикс24 (стенд)",
            mode=ConnectorMode.PER_USER.value,
            modules=["disk", "disk_personal"],
            config={"portal": self.portal, "client_id": "stand"},
            credentials=self.secrets.encrypt({"client_secret": "stand"}),
            credentials_set_at=datetime.now(UTC),
        )
        session.add(connector)
        await session.commit()
        session.add(
            ConnectorUserGrant(
                connector_id=connector.id,
                user_id=employee.id,
                credentials=self.secrets.encrypt({"webhook": WEBHOOK}),
                external_user_id="1",
            )
        )
        await session.commit()
        return connector

    def sync_service(
        self,
        raw: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> ConnectorSyncService:
        return ConnectorSyncService(
            session_maker,
            OutboundClient(raw, via_proxy=VIA_PROXY),
            default_registry(self.settings),
            self.secrets,
            self.settings,
        )
