"""Клиент протокола агент → kronto (docs/SMB-AGENT.md, §3).

Ручки под `/api/v1/agent`, ключ — `Authorization: Bearer kag_…`:
GET /state, POST /runs, PUT /documents (multipart: meta + file),
POST /access (до 500 документов), POST /runs/{id}/finish.

Повторы: 429 и 5xx, а также сбои сети — с паузой (`Retry-After` в
секундах или датой HTTP; без него — 1, 2, 4… с, не больше минуты),
до max_attempts попыток. 401 — неверный ключ, 403 — подключение
остановлено или вид скрыт: обход прекращается сразу, без повторов.
"""

import json
import logging
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from kronto_agent import __version__

log = logging.getLogger(__name__)

ACCESS_BATCH = 500
STATE_PAGE = 1000
MAX_RETRY_AFTER = 300.0


class KrontoError(Exception):
    """kronto отказал или недоступен."""


class KrontoAuthError(KrontoError):
    """401: ключ агента неверный или отозван."""


class KrontoForbiddenError(KrontoError):
    """403: подключение остановлено или вид скрыт у компании."""


class KrontoUnavailableError(KrontoError):
    """Повторы исчерпаны (сеть, 429, 5xx)."""


@dataclass(frozen=True)
class PutResult:
    # added | updated | unchanged — принят; too_large (413),
    # unsupported (415), rejected (прочие 4xx) — пропущен.
    status: str
    material_id: str | None = None
    detail: str = ""


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After: число секунд или дата HTTP (RFC 9110, 10.2.3)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, min(delta, MAX_RETRY_AFTER))


class KrontoClient:
    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        ca_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 6,
        timeout: float = 120.0,
    ) -> None:
        verify: ssl.SSLContext | bool = True
        if ca_file:
            verify = ssl.create_default_context(cafile=ca_file)
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1/agent",
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": f"kronto-agent/{__version__}",
            },
            timeout=timeout,
            verify=verify,
            transport=transport,
            follow_redirects=False,
        )
        self._sleep = sleep
        self._max_attempts = max_attempts

    def close(self) -> None:
        self._http.close()

    # --- Повторы ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        delay = 1.0
        last = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
                wait = delay
            else:
                if resp.status_code == 401:
                    raise KrontoAuthError("kronto: ключ агента не принят (401)")
                if resp.status_code == 403:
                    raise KrontoForbiddenError(
                        "kronto: подключение остановлено или недоступно (403)"
                    )
                if resp.status_code != 429 and resp.status_code < 500:
                    return resp
                last = f"HTTP {resp.status_code}"
                hinted = retry_after_seconds(resp.headers.get("Retry-After"))
                wait = hinted if hinted is not None else delay
            if attempt == self._max_attempts:
                break
            log.warning(
                "kronto %s %s: %s, повтор %d через %.0f с",
                method,
                path,
                last,
                attempt,
                wait,
            )
            self._sleep(wait)
            delay = min(delay * 2, 60.0)
        raise KrontoUnavailableError(f"kronto {method} {path}: {last}")

    @staticmethod
    def _json(resp: httpx.Response, what: str) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise KrontoError(
                f"kronto {what}: HTTP {resp.status_code} {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise KrontoError(f"kronto {what}: ответ не JSON") from exc
        if not isinstance(data, dict):
            raise KrontoError(f"kronto {what}: ответ не объект")
        return data

    # --- Ручки ------------------------------------------------------------

    def state(self) -> dict[str, str]:
        """Что уже есть в kronto: external_id → version (все страницы)."""
        out: dict[str, str] = {}
        after: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": STATE_PAGE}
            if after:
                params["after"] = after
            data = self._json(self._request("GET", "/state", params=params), "state")
            for doc in data.get("documents") or []:
                out[str(doc["external_id"])] = str(doc.get("version") or "")
            nxt = data.get("next")
            if not nxt or nxt == after:
                return out
            after = str(nxt)

    def start_run(self) -> str:
        data = self._json(self._request("POST", "/runs"), "runs")
        return str(data["run_id"])

    def put_document(
        self, meta: dict[str, Any], filename: str, content: bytes
    ) -> PutResult:
        files = {
            "meta": (
                None,
                json.dumps(meta, ensure_ascii=False).encode(),
                "application/json",
            ),
            "file": (filename, content, "application/octet-stream"),
        }
        resp = self._request("PUT", "/documents", files=files)
        if resp.status_code == 413:
            return PutResult("too_large", detail=resp.text[:200])
        if resp.status_code == 415:
            return PutResult("unsupported", detail=resp.text[:200])
        if resp.status_code >= 400:
            return PutResult(
                "rejected", detail=f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        data = self._json(resp, "documents")
        material = data.get("material_id")
        return PutResult(
            str(data.get("status") or "added"),
            material_id=str(material) if material is not None else None,
        )

    def access(self, run_id: str, documents: list[dict[str, Any]]) -> list[str]:
        """Права неизменённых файлов; возвращает неизвестные kronto external_id."""
        unknown: list[str] = []
        for i in range(0, len(documents), ACCESS_BATCH):
            chunk = documents[i : i + ACCESS_BATCH]
            resp = self._request(
                "POST", "/access", json={"run_id": run_id, "documents": chunk}
            )
            data = self._json(resp, "access")
            unknown.extend(str(x) for x in data.get("unknown") or [])
        return unknown

    def finish(self, run_id: str, complete: bool, stats: dict[str, Any]) -> None:
        resp = self._request(
            "POST",
            f"/runs/{run_id}/finish",
            json={"complete": complete, "stats": stats},
        )
        if resp.status_code >= 400:
            raise KrontoError(
                f"kronto finish: HTTP {resp.status_code} {resp.text[:200]}"
            )
