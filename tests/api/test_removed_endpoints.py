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
        # Сеансы переехали в /auth/sessions (этап 11): под /account
        # refresh-cookie не приходила, и «это устройство» не отмечалось.
        ("GET", "/api/v1/account/sessions"),
        ("POST", "/api/v1/account/sessions/00000000-0000-0000-0000-000000000000/end"),
    ],
)
async def test_removed_endpoint_is_not_routed(
    api: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await api.request(method, path, json={})

    # 405 — путь совпал с шаблоном другой ручки (PATCH /users/{user_id}),
    # но такого действия нет. Для клиента это тот же «не существует».
    assert response.status_code in (404, 405)


def test_no_route_mentions_old_concept() -> None:
    # Пути ручек — из схемы OpenAPI: с FastAPI 0.142 app.routes отдаёт
    # подключённые роутеры целиком, без путей их ручек.
    paths = {getattr(route, "path", "") for route in app.routes}
    paths |= set(app.openapi()["paths"])

    for word in ("program", "brief", "intern"):
        assert not any(word in path for path in paths), word
    # Регистрация вернулась одна и намеренно (ТЗ §2, 03.10): учётка без
    # компании, подтверждение почты, лимит по IP. Старая /users/register
    # пускала в компанию без проверки — её быть не должно.
    assert [path for path in sorted(paths) if "register" in path] == [
        "/api/v1/auth/register"
    ]
