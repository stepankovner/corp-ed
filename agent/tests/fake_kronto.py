"""Поддельный kronto для тестов агента: ручки /api/v1/agent из
docs/SMB-AGENT.md (§3) на httpx.MockTransport.

Хранит документы подключения (external_id → версия, почты, содержимое) и
повторяет поведение сервера, на которое агент опирается: /state
страницами по `after`/`limit`, один открытый обход, /access возвращает
неизвестные external_id, finish(complete=true) удаляет документы, которых
обход не коснулся. Сбои задаются заранее: `fail_next` — очередь ответов
(429 с Retry-After, 503…), `reject_files` — имя файла → код (413/415).
"""

import json
import uuid
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx

KEY = "kag_tenant_conn_secret"
PREFIX = "/api/v1/agent"


@dataclass
class Doc:
    version: str
    allowed_emails: list[str]
    meta: dict[str, Any]
    content: bytes
    touched_run: str | None = None


@dataclass
class FakeKronto:
    key: str = KEY
    docs: dict[str, Doc] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_run: str | None = None
    fail_next: list[httpx.Response] = field(default_factory=list)
    reject_files: dict[str, int] = field(default_factory=dict)
    forbidden: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)
    puts: list[dict[str, Any]] = field(default_factory=list)
    access_calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def seed(self, external_id: str, version: str, emails: list[str]) -> None:
        self.docs[external_id] = Doc(version, emails, {}, b"")

    # --- Обработка ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.fail_next:
            return self.fail_next.pop(0)
        if request.headers.get("Authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"detail": "bad key"})
        if self.forbidden:
            return httpx.Response(403, json={"detail": "connector paused"})
        if not path.startswith(PREFIX):
            return httpx.Response(404)
        route = path[len(PREFIX) :]
        if request.method == "GET" and route == "/state":
            return self._state(request)
        if request.method == "POST" and route == "/runs":
            return self._start()
        if request.method == "PUT" and route == "/documents":
            return self._put(request)
        if request.method == "POST" and route == "/access":
            return self._access(request)
        if request.method == "POST" and route.startswith("/runs/"):
            return self._finish(route.split("/")[2], json.loads(request.content))
        return httpx.Response(404)

    def _state(self, request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        limit = int(request.url.params.get("limit", "1000"))
        ids = sorted(i for i in self.docs if after is None or i > after)
        page = ids[:limit]
        nxt = page[-1] if len(ids) > limit else None
        docs = [{"external_id": i, "version": self.docs[i].version} for i in page]
        return httpx.Response(200, json={"documents": docs, "next": nxt})

    def _start(self) -> httpx.Response:
        if self.open_run:
            self.runs[self.open_run]["status"] = "failed"
        run_id = str(uuid.uuid4())
        self.runs[run_id] = {"status": "running"}
        self.open_run = run_id
        return httpx.Response(
            201, json={"run_id": run_id, "started_at": "2026-10-15T09:00:00Z"}
        )

    def _put(self, request: httpx.Request) -> httpx.Response:
        head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        message = BytesParser(policy=HTTP).parsebytes(head + request.content)
        parts: dict[str, tuple[bytes, str | None]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            data = payload if isinstance(payload, bytes) else b""
            parts[str(name)] = (data, part.get_filename())
        meta = json.loads(parts["meta"][0])
        content, _filename = parts["file"]
        self.puts.append(meta)
        if meta.get("run_id") != self.open_run:
            return httpx.Response(409, json={"detail": "run is not open"})
        code = self.reject_files.get(meta["filename"])
        if code:
            return httpx.Response(code, json={"detail": "rejected"})
        ext = meta["external_id"]
        old = self.docs.get(ext)
        status = "added" if old is None else "updated"
        self.docs[ext] = Doc(
            version=meta["version"],
            allowed_emails=list(meta["allowed_emails"]),
            meta=meta,
            content=content,
            touched_run=self.open_run,
        )
        return httpx.Response(200, json={"status": status, "material_id": ext})

    def _access(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("run_id") != self.open_run:
            return httpx.Response(409, json={"detail": "run is not open"})
        documents = body["documents"]
        assert len(documents) <= 500, "больше 500 документов в одном /access"
        self.access_calls.append(documents)
        unknown = []
        updated = 0
        for item in documents:
            doc = self.docs.get(item["external_id"])
            if doc is None:
                unknown.append(item["external_id"])
                continue
            doc.allowed_emails = list(item["allowed_emails"])
            doc.touched_run = self.open_run
            updated += 1
        return httpx.Response(200, json={"updated": updated, "unknown": unknown})

    def _finish(self, run_id: str, body: dict[str, Any]) -> httpx.Response:
        if run_id not in self.runs or run_id != self.open_run:
            return httpx.Response(409, json={"detail": "run is not open"})
        self.runs[run_id] = {"status": "done", **body}
        self.open_run = None
        if body.get("complete"):
            for ext in [e for e, d in self.docs.items() if d.touched_run != run_id]:
                del self.docs[ext]
        return httpx.Response(200, json={"ok": True})

    # --- Для проверок ------------------------------------------------------

    def visible_to(self, email: str) -> set[str]:
        return {e for e, d in self.docs.items() if email in d.allowed_emails}
