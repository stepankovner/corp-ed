"""Вход сотрудника в OAuth-систему для приёмки (Битрикс24, Яндекс 360, amoCRM).

У таких систем учётные данные сотрудника — токены, которые выдаёт вход в
браузере. Этот помощник проходит ту же дорогу, что кнопка «Подключить» в
kronto, но без API: печатает адрес входа, ждёт код из адреса возврата,
обменивает его тем же `OAuthFlow` вида и записывает токены сотрудника в
файл учётных данных (вне репозитория, 0600).

    python -m tests.acceptance.oauth_login --system bitrix24 --login vetrov

Перед этим в файле (или ACC_<СИСТЕМА>_… переменных) должны быть config
подключения (адрес портала, client_id) и секрет приложения (app). Адрес
возврата приложения — любой, зарегистрированный в нём, например
https://stage.krontoai.ru/api/v1/connectors/oauth/callback: после входа
браузер откроет его, нужный `code=…` — в адресной строке (страница может
отвечать ошибкой — это не мешает).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from corp_ed.core import outbound
from tests.acceptance.model import load_manifest
from tests.acceptance.scenario import default_registry_for, make_settings, new_key
from tests.acceptance.systems import resolve_credentials
from tests.acceptance.systems.base import credentials_path, write_credentials

STATE = "kronto-acceptance"


def extract_code(value: str) -> str:
    """Код из вставленной строки: сам код или адрес возврата целиком."""
    value = value.strip()
    if "code=" in value:
        query = parse_qs(urlsplit(value).query)
        return query["code"][0]
    return value


async def _login(system_id: str, login: str, path: Path | None) -> int:
    manifest = load_manifest()
    system = manifest.systems[system_id]
    creds = resolve_credentials(system, manifest, path)
    if creds is None or not creds.config:
        print("нет config подключения в учётных данных", file=sys.stderr)
        return 2
    key = new_key()
    registry = default_registry_for(key)
    settings = make_settings(key)
    raw = outbound.outbound_http_client(via_proxy=settings.outbound_via_proxy)
    try:
        http = outbound.OutboundClient(raw, via_proxy=settings.outbound_via_proxy)
        flow = registry.build_oauth(system.kind, creds.config, creds.app or {}, http)
        print("Откройте адрес, войдите как", manifest.employees[login].email)
        print(flow.authorize_url(STATE))
        code = extract_code(input("Вставьте code или адрес возврата целиком: "))
        exchanged = await flow.exchange(code)
    finally:
        await raw.aclose()
    creds.users[login] = dict(exchanged.credentials)
    if path is None:
        print("файл учётных данных не задан, токены не сохранены", file=sys.stderr)
        return 2
    write_credentials(path, system_id, creds)
    print(f"токены {login} записаны в {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.acceptance.oauth_login")
    parser.add_argument("--system", required=True)
    parser.add_argument("--login", required=True)
    parser.add_argument("--credentials", default="~/.kronto-acc/credentials.json")
    args = parser.parse_args(argv)
    return asyncio.run(
        _login(args.system, args.login, credentials_path(args.credentials))
    )


if __name__ == "__main__":
    sys.exit(main())
