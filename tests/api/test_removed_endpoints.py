"""Ручки старой концепции удалены и не должны вернуться незаметно.

Каждая лишняя ручка — поверхность атаки для пентеста: /users/register
принимал запросы без аутентификации, /programs/generate тратил деньги
на LLM, /auth/manager-only был отладочной заглушкой.
"""

import httpx
import pytest

from corp_ed.main import app


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/users/register"),
        ("GET", "/api/v1/auth/manager-only"),
        ("POST", "/api/v1/programs/generate"),
        ("GET", "/api/v1/programs/00000000-0000-0000-0000-000000000000"),
    ],
)
async def test_removed_endpoint_is_not_routed(
    api: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await api.request(method, path, json={})

    assert response.status_code == 404


def test_no_route_mentions_old_concept() -> None:
    paths = [getattr(route, "path", "") for route in app.routes]

    for word in ("program", "brief", "intern", "register"):
        assert not any(word in path for path in paths), word
