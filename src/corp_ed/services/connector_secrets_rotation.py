"""Ротация ключа шифрования учётных данных источников.

`cli rotate-connector-secrets`: после того как новый ключ дописан первым
в CONNECTOR_SECRETS_KEYS, все шифротексты перешифровываются им — и
старый ключ можно убрать. По компаниям в своём tenant_scope (таблицы под
RLS). Запись, которую не расшифровал ни один ключ, не трогается и
попадает в лог: такой коннектор остановится сам с кодом
credentials_unreadable, и админ введёт данные заново.
"""

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from corp_ed.core.secrets import SecretBox, SecretDecryptionError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorUserGrant
from corp_ed.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger()


@dataclass(frozen=True)
class RotationReport:
    connectors: int
    grants: int
    unreadable: int = 0


class ConnectorSecretsRotation:
    def __init__(
        self, session_maker: async_sessionmaker[AsyncSession], secrets: SecretBox
    ) -> None:
        self.session_maker = session_maker
        self.secrets = secrets

    async def rotate(self) -> RotationReport:
        async with self.session_maker() as session:
            tenants = await TenantRepository(session).list_all()
        connectors = grants = unreadable = 0
        for tenant in tenants:
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    rows = await session.scalars(
                        select(Connector)
                        .options(undefer(Connector.credentials))
                        .where(Connector.credentials.is_not(None))
                    )
                    for connector in rows:
                        assert connector.credentials is not None  # noqa: S101 — фильтр выше
                        try:
                            connector.credentials = self.secrets.rotate(
                                connector.credentials
                            )
                            connectors += 1
                        except SecretDecryptionError:
                            unreadable += 1
                            logger.error(
                                "connector_secret_unreadable",
                                connector_id=str(connector.id),
                            )
                    grant_rows = await session.scalars(
                        select(ConnectorUserGrant).options(
                            undefer(ConnectorUserGrant.credentials)
                        )
                    )
                    for grant in grant_rows:
                        try:
                            grant.credentials = self.secrets.rotate(grant.credentials)
                            grants += 1
                        except SecretDecryptionError:
                            unreadable += 1
                            logger.error(
                                "grant_secret_unreadable", grant_id=str(grant.id)
                            )
                    await session.commit()
        logger.info(
            "connector_secrets_rotated",
            connectors=connectors,
            grants=grants,
            unreadable=unreadable,
        )
        return RotationReport(connectors, grants, unreadable)
