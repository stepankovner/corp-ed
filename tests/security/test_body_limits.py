"""Потолки тела запроса по путям: JSON, файлы, документы (решение 09.10)."""

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from corp_ed.core.middleware import BodySizeLimitMiddleware


async def _echo(request: Request) -> PlainTextResponse:
    return PlainTextResponse(str(len(await request.body())))


def _client() -> httpx.AsyncClient:
    app = Starlette(
        routes=[
            Route("/api/v1/materials/upload", _echo, methods=["POST"]),
            Route("/api/v1/account/avatar", _echo, methods=["POST"]),
            Route("/api/v1/faq/ask", _echo, methods=["POST"]),
        ]
    )
    wrapped = BodySizeLimitMiddleware(
        app,
        max_body_bytes=10,
        max_upload_bytes=100,
        upload_paths=("/materials/upload", "/account/avatar"),
        max_large_upload_bytes=1000,
        large_upload_paths=("/materials/upload",),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
    )


async def test_document_upload_gets_large_ceiling() -> None:
    async with _client() as client:
        ok = await client.post("/api/v1/materials/upload", content=b"x" * 500)
        too_big = await client.post("/api/v1/materials/upload", content=b"x" * 1001)
    assert ok.status_code == 200
    assert too_big.status_code == 413


async def test_other_uploads_keep_file_limit() -> None:
    async with _client() as client:
        response = await client.post("/api/v1/account/avatar", content=b"x" * 500)
    assert response.status_code == 413


async def test_json_keeps_body_limit() -> None:
    async with _client() as client:
        response = await client.post("/api/v1/faq/ask", content=b"x" * 50)
    assert response.status_code == 413


async def test_large_ceiling_never_below_file_limit() -> None:
    middleware = BodySizeLimitMiddleware(
        _echo,  # type: ignore[arg-type]
        max_body_bytes=10,
        max_upload_bytes=100,
        upload_paths=(),
        max_large_upload_bytes=50,
    )
    assert middleware.max_large_upload_bytes == 100
