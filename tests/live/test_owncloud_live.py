"""Живая проверка вида `owncloud`: официальный образ owncloud/server
(ownCloud Server 10) в Docker с той же компанией, что у Nextcloud. Сами
тесты — tests/live/oc_family.py.

Как поднять стенд — tests/live/owncloud/README.md. Без переменных — пропуск:

    OWNCLOUD_URL=https://127.0.0.1:8445/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_owncloud_live.py -q

Пароль приложения ownCloud выдаёт только веб-интерфейс: тесты входят
формой и создают его из сессии (tests/live/owncloud/seed.py).
"""

import os

import pytest

from tests.live.dav_stand import CA
from tests.live.oc_family import SharedDriveSuite
from tests.live.owncloud.seed import app_password

URL = os.environ.get("OWNCLOUD_URL", "")

pytestmark = pytest.mark.skipif(
    not (URL and CA),
    reason="нужны OWNCLOUD_URL и DAV_STAND_CA (стенд ownCloud в Docker)",
)


class TestOwncloud(SharedDriveSuite):
    url = URL
    kind = "owncloud"
    app_password = app_password
