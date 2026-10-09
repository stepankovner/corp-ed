"""Общее для живых стендов WebDAV-дисков: NAS (Apache mod_dav), Nextcloud,
ownCloud, Seafile. Каждый стенд — своя папка в tests/live/ и свой тестовый
файл; здесь — то, что у них одинаково.

Почему стенд по HTTPS, а не по http, как Confluence: адаптер WebDAV сам
собирает адреса с https:// и не верит ссылкам (href) с другой схемой —
по http он не увидел бы ни одного файла. Поэтому у каждого стенда TLS
на 127.0.0.1 с самоподписанным сертификатом (`tls`), а проверка адреса
пропускает ровно этот стенд (`local_stand`): всё остальное — как в бою.

    python -m tests.live.dav_stand tls ~/dav-tls       # cert.pem и key.pem
    python -m tests.live.dav_stand check --stand https://127.0.0.1:8443 \\
        --ca ~/dav-tls/cert.pem -- --kind webdav --config server=… …

`check` — тот же `cli connector-check` (тот же код, разбор аргументов и
запись ответов), только адрес стенда принимается, а сертификату стенда
доверяют.
"""

import argparse
import asyncio
import ipaddress
import os
import ssl
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Connector, ConnectorUserGrant
from corp_ed.domain.types import ConnectorMode, SyncTrigger
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome

PASSWORD = "Kronto-Test-1"
"""Пароль тестовых пользователей стендов: выдуманная компания в контейнере
на 127.0.0.1, как у стенда Confluence."""
CA = os.environ.get("DAV_STAND_CA", "")
"""Сертификат стенда (cert.pem из `tls`): ему доверяет клиент тестов."""


# --- TLS ------------------------------------------------------------------------


def write_tls(directory: Path) -> None:
    """Самоподписанный сертификат на IP 127.0.0.1 (30 дней) и ключ к нему."""
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kronto-stand")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = directory / "key.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # Ключ читает процесс в контейнере под своим пользователем.
    key_path.chmod(0o644)


def trust(ca: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca)


def stand_http(ca: str = CA) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=trust(ca))


# --- адрес стенда -----------------------------------------------------------------


@contextmanager
def local_stand(url: str) -> Iterator[None]:
    """Пропустить проверку адреса только для стенда (https, 127.0.0.1:порт):
    в бою адаптер ходит лишь на публичные адреса (core/outbound.py)."""
    import corp_ed.cli as cli

    original = outbound.validate_outbound_url
    stand = httpx.URL(url)

    async def validate(
        target_url: str, *, resolver: outbound.Resolver | None = None
    ) -> outbound.OutboundTarget:
        target = httpx.URL(target_url)
        if (target.scheme, target.host, target.port) == (
            "https",
            stand.host,
            stand.port,
        ):
            return outbound.OutboundTarget(
                url=target_url,
                host=target.host,
                port=target.port or 443,
                address=target.host,
            )
        return await original(target_url, resolver=resolver)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(outbound, "validate_outbound_url", validate)
        patch.setattr(cli, "validate_outbound_url", validate)
        yield


# --- наполнение по WebDAV ------------------------------------------------------


class DavSeeder:
    """Клиент WebDAV от имени пользователя стенда: папки, файлы, удаление.

    root — корень пользователя (…/remote.php/dav/files/<uid>/ у Nextcloud),
    пути — от него, через «/»."""

    def __init__(self, root: str, login: str, password: str, ca: str = CA) -> None:
        self.root = root if root.endswith("/") else root + "/"
        self._http = httpx.Client(auth=(login, password), verify=trust(ca), timeout=60)

    def close(self) -> None:
        self._http.close()

    def url(self, path: str) -> str:
        return self.root + "/".join(quote(part) for part in path.split("/") if part)

    def mkdir(self, path: str) -> None:
        """MKCOL с родителями; уже существующая папка — не ошибка."""
        parts = [part for part in path.split("/") if part]
        for end in range(1, len(parts) + 1):
            folder = self.url("/".join(parts[:end])) + "/"
            response = self._http.request("MKCOL", folder)
            if response.status_code not in (201, 405):
                response.raise_for_status()

    def put(self, path: str, data: bytes) -> None:
        folder = path.rpartition("/")[0]
        if folder:
            self.mkdir(folder)
        response = self._http.put(self.url(path), content=data)
        if response.status_code not in (200, 201, 204):
            response.raise_for_status()

    def delete(self, path: str) -> None:
        response = self._http.delete(self.url(path))
        if response.status_code in (301, 302):
            # Папка без «/» на конце: Apache отвечает редиректом на адрес со «/».
            response = self._http.delete(self.url(path) + "/")
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()

    def exists(self, path: str) -> bool:
        response = self._http.request(
            "PROPFIND", self.url(path), headers={"Depth": "0"}
        )
        return response.status_code == 207


def deep_path(top: str, levels: int) -> str:
    """top/1/2/…/levels — папка на глубине levels от top."""
    return "/".join([top, *(str(level) for level in range(1, levels + 1))])


# --- синхронизация per_user ---------------------------------------------------


class PerUserStand:
    """Подключение per_user и гранты сотрудников с их логином и паролем —
    как после «Подключить» в кабинете сотрудника."""

    def __init__(self, url: str, ca: str = CA) -> None:
        key = Fernet.generate_key().decode()
        self.url = url
        self.ca = ca
        self.secrets = SecretBox([key])
        self.settings = ConnectorSettings(secrets_keys=key)  # type: ignore[arg-type]

    async def connector(
        self,
        session: AsyncSession,
        kind: str,
        config: dict[str, Any],
        app_credentials: dict[str, str] | None = None,
    ) -> Connector:
        """app_credentials — секрет клиента OAuth (Nextcloud OAuth2)."""
        connector = Connector(
            kind=kind,
            name=f"{kind} (стенд)",
            mode=ConnectorMode.PER_USER.value,
            modules=["files"],
            config=config,
            credentials=(
                self.secrets.encrypt(app_credentials) if app_credentials else None
            ),
            credentials_set_at=datetime.now(UTC),
        )
        session.add(connector)
        await session.commit()
        return connector

    async def grant(
        self,
        session: AsyncSession,
        connector: Connector,
        user_id: Any,
        credentials: dict[str, str],
    ) -> ConnectorUserGrant:
        grant = ConnectorUserGrant(
            connector_id=connector.id,
            user_id=user_id,
            credentials=self.secrets.encrypt(credentials),
        )
        session.add(grant)
        await session.commit()
        return grant

    async def sync(
        self, session_maker: async_sessionmaker[AsyncSession], connector: Connector
    ) -> SyncOutcome:
        with local_stand(self.url):
            async with stand_http(self.ca) as raw:
                service = ConnectorSyncService(
                    session_maker,
                    outbound.OutboundClient(raw),
                    default_registry(self.settings),
                    self.secrets,
                    self.settings,
                )
                outcome = await service.run(
                    connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
                )
        assert outcome is not None
        return outcome


# --- командная строка -------------------------------------------------------------


async def _check(stand: str, ca: str, argv: Sequence[str]) -> int:
    import corp_ed.cli as cli

    args = cli._parser().parse_args(["connector-check", *argv])
    with local_stand(stand):
        async with stand_http(ca) as raw:
            return await cli._connector_check(args, http=outbound.OutboundClient(raw))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.live.dav_stand")
    commands = parser.add_subparsers(dest="command", required=True)
    tls = commands.add_parser("tls", help="сертификат стенда на 127.0.0.1")
    tls.add_argument("directory", type=Path)
    check = commands.add_parser("check", help="cli connector-check против стенда")
    check.add_argument("--stand", required=True, help="https://127.0.0.1:порт")
    check.add_argument("--ca", required=True, help="cert.pem стенда")
    check.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "tls":
        write_tls(args.directory)
        print(f"{args.directory}/cert.pem, {args.directory}/key.pem")
        return 0
    rest = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    return asyncio.run(_check(args.stand, args.ca, rest))


if __name__ == "__main__":
    sys.exit(main())
