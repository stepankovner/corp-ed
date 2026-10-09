"""Поддельный публичный сайт для контрактных тестов вида website.

robots.txt, карты сайта (в том числе индекс и gzip), HTML-страницы,
файлы, редиректы, ETag и Last-Modified, лимит частоты (429 с
Retry-After), HEAD, который сайт может не поддерживать. Каждый запрос
пишется в журнал: метод, путь, User-Agent — тесты проверяют, что лишних
запросов нет и что агент представляется.
"""

from dataclasses import dataclass, field

import httpx

from corp_ed.core.outbound import OutboundClient
from tests.fake_connector import public_resolver

HOST = "www.example.ru"
BASE = f"https://{HOST}/"


@dataclass
class Resource:
    body: bytes
    content_type: str = "text/html; charset=utf-8"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def page(
    title: str,
    body: str,
    *,
    links: tuple[str, ...] = (),
    head: str = "",
) -> bytes:
    anchors = "".join(f'<a href="{href}">{href}</a> ' for href in links)
    return (
        f"<!doctype html><html><head><title>{title}</title>{head}</head>"
        f"<body><nav>Меню</nav><main><h1>{title}</h1><p>{body}</p>"
        f"<p>{anchors}</p></main><footer>©</footer></body></html>"
    ).encode()


@dataclass
class FakeSite:
    host: str = HOST
    robots: str | int | None = None
    """Текст robots.txt; число — статус ответа; None — 404."""
    resources: dict[str, Resource] = field(default_factory=dict)
    """Путь с query → ответ."""
    head_supported: bool = True
    rate_limit: dict[str, int] = field(default_factory=dict)
    """Путь → сколько раз ответить 429 перед обычным ответом."""
    retry_after: str = "3"
    log: list[tuple[str, str]] = field(default_factory=list)
    agents: set[str] = field(default_factory=set)

    def client(self) -> OutboundClient:
        return OutboundClient(
            httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            resolver=public_resolver,
        )

    def add(self, path: str, body: bytes, **kwargs: object) -> str:
        self.resources[path] = Resource(body, **kwargs)  # type: ignore[arg-type]
        return f"https://{self.host}{path}"

    def redirect(self, path: str, location: str, status: int = 301) -> None:
        self.resources[path] = Resource(
            b"", status=status, headers={"location": location}
        )

    def calls(self, method: str | None = None) -> list[str]:
        return [path for m, path in self.log if method is None or m == method]

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        target = request.url.raw_path.decode()
        self.log.append((request.method, target))
        self.agents.add(request.headers.get("user-agent", ""))
        if host != self.host:
            return httpx.Response(404, text="unknown host")
        hits = self.rate_limit.get(target, 0)
        if hits:
            self.rate_limit[target] = hits - 1
            return httpx.Response(429, headers={"retry-after": self.retry_after})
        if target == "/robots.txt":
            if isinstance(self.robots, int):
                return httpx.Response(self.robots, text="error")
            if self.robots is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                200, text=self.robots, headers={"content-type": "text/plain"}
            )
        resource = self.resources.get(target)
        if resource is None:
            return httpx.Response(404, text="<html>Нет страницы</html>")
        if request.method == "HEAD" and not self.head_supported:
            return httpx.Response(405)
        headers = {"content-type": resource.content_type, **resource.headers}
        etag = resource.headers.get("etag")
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers=headers)
        body = b"" if request.method == "HEAD" else resource.body
        if request.method == "HEAD":
            headers["content-length"] = str(len(resource.body))
        return httpx.Response(resource.status, content=body, headers=headers)
