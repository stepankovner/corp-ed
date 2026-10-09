"""Выдуманная компания в ownCloud 10 для живой проверки вида `owncloud` —
та же, что у Nextcloud (tests/live/nextcloud/seed.py: пользователи,
группа, файлы и шары; у ownCloud 10 те же OCS и WebDAV).

    OWNCLOUD_URL=https://127.0.0.1:8445/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        python -m tests.live.owncloud.seed

Пароль приложения ownCloud выдаёт только веб-интерфейс («Настройки →
Безопасность → Новое приложение»): `app_password` делает то же самое —
входит формой и создаёт токен из сессии.
"""

import os
import re
import sys

import httpx

from tests.live.dav_stand import CA, PASSWORD, trust
from tests.live.nextcloud.seed import FILES, SHARES, USERS, seed

_REQUEST_TOKEN = re.compile(r'data-requesttoken="([^"]+)"')


def _request_token(page: str) -> str:
    found = _REQUEST_TOKEN.search(page)
    if found is None:
        raise RuntimeError("на странице нет requesttoken")
    return found.group(1)


def app_password(url: str, user: str, password: str = PASSWORD, ca: str = CA) -> str:
    base = url.rstrip("/")
    with httpx.Client(verify=trust(ca), timeout=60, follow_redirects=True) as http:
        login = http.get(f"{base}/index.php/login")
        login.raise_for_status()
        signed_in = http.post(
            f"{base}/index.php/login",
            data={
                "user": user,
                "password": password,
                "requesttoken": _request_token(login.text),
            },
        )
        signed_in.raise_for_status()
        settings = http.get(f"{base}/index.php/settings/personal?sectionid=security")
        settings.raise_for_status()
        created = http.post(
            f"{base}/index.php/settings/personal/authtokens",
            json={"name": "kronto-stand"},
            headers={"requesttoken": _request_token(settings.text)},
        )
        created.raise_for_status()
        return str(created.json()["token"])


if __name__ == "__main__":
    address = os.environ.get("OWNCLOUD_URL", "")
    if not address:
        sys.exit("нужен OWNCLOUD_URL (https://127.0.0.1:8445/)")
    seed(address)
    print(f"пользователей: {len(USERS)}, файлов: {len(FILES)}, шар: {len(SHARES)}")
