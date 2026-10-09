"""Живая проверка вида `nextcloud` (пароль приложения): официальный образ
Nextcloud в Docker, три пользователя, группа и общий доступ к папкам и
файлу — пользователю и группе сразу. Сами тесты — tests/live/oc_family.py
(общие с ownCloud).

Как поднять стенд — tests/live/nextcloud/README.md (сертификат, compose,
seed.py). Без переменных — пропуск:

    NEXTCLOUD_URL=https://127.0.0.1:8444/ DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_nextcloud_live.py -q

Пароли приложений тесты получают сами из паролей входа
(core/getapppassword) — то же, что сотрудник создаёт в настройках.
"""

import os

import pytest

from tests.live.dav_stand import CA
from tests.live.nextcloud.seed import app_password
from tests.live.oc_family import SharedDriveSuite

URL = os.environ.get("NEXTCLOUD_URL", "")

pytestmark = pytest.mark.skipif(
    not (URL and CA),
    reason="нужны NEXTCLOUD_URL и DAV_STAND_CA (стенд Nextcloud в Docker)",
)


class TestNextcloud(SharedDriveSuite):
    url = URL
    kind = "nextcloud"
    app_password = app_password
