"""HTTP-клиент к API corp-ed для eval — граница с бэкендом (A6).

run_eval ходит только сюда, в БД и во внутренности бэкенда не лезет.

Эндпоинты:
- POST /api/v1/auth/login  {company_code, email, password} → {access_token}
- POST /api/v1/faq/search  {question, limit} → top-K чанков с расстояниями,
  без LLM. Отладочный, только для админа. ЗАДАЧА БЭКЕНДУ (до 1.10);
  предлагаемый ответ — в docs/backend-handoff.md (BH-5).
- POST /api/v1/faq/ask     {question} → {content, answer_given, sources}

Разбор ответов терпим к именам полей (chunk_id / id, llm_text / content,
material_title / title, heading_path списком или строкой «A > B»): контракт
/faq/search ещё не зафиксирован, а бэкенд переписывается.

Настройки — из окружения (или .env):
    CORP_ED_BASE_URL   по умолчанию http://localhost:8000
    CORP_ED_TOKEN      готовый JWT; иначе логин по трём переменным ниже
    CORP_ED_COMPANY, CORP_ED_EMAIL, CORP_ED_PASSWORD
"""

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from eval.relevance import RetrievedChunk

DEFAULT_BASE_URL = "http://localhost:8000"
API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class AskResult:
    content: str
    answer_given: bool
    sources: list[RetrievedChunk]
    latency_ms: float
    extra: dict[str, Any] = field(default_factory=dict)


class CorpEdClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        token: str | None = None,
        http: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout)
        self._token = token

    @classmethod
    def from_env(cls) -> "CorpEdClient":
        _load_dotenv()
        client = cls(
            os.environ.get("CORP_ED_BASE_URL", DEFAULT_BASE_URL),
            token=os.environ.get("CORP_ED_TOKEN") or None,
        )
        if client._token is None:
            try:
                client.login(
                    os.environ["CORP_ED_COMPANY"],
                    os.environ["CORP_ED_EMAIL"],
                    os.environ["CORP_ED_PASSWORD"],
                )
            except KeyError as error:
                raise SystemExit(
                    f"Не задан {error}: нужен CORP_ED_TOKEN или "
                    "CORP_ED_COMPANY + CORP_ED_EMAIL + CORP_ED_PASSWORD"
                ) from None
        return client

    def login(self, company_code: str, email: str, password: str) -> None:
        response = self._http.post(
            f"{API_PREFIX}/auth/login",
            json={"company_code": company_code, "email": email, "password": password},
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]

    def search(self, question: str, limit: int) -> tuple[list[RetrievedChunk], float]:
        body, latency = self._post(
            "/faq/search", {"question": question, "limit": limit}
        )
        return parse_search_response(body), latency

    def ask(self, question: str) -> AskResult:
        body, latency = self._post("/faq/ask", {"question": question})
        if not isinstance(body, Mapping):
            raise ValueError(f"unexpected /faq/ask response: {body!r}")
        known = {"content", "answer_given", "sources"}
        return AskResult(
            content=str(body.get("content", "")),
            answer_given=bool(body.get("answer_given", False)),
            sources=parse_chunks(body.get("sources", [])),
            latency_ms=latency,
            extra={key: value for key, value in body.items() if key not in known},
        )

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[Any, float]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        started = time.perf_counter()
        response = self._http.post(f"{API_PREFIX}{path}", json=payload, headers=headers)
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        return response.json(), latency_ms


def parse_search_response(body: Any) -> list[RetrievedChunk]:
    """Ответ /faq/search → чанки. Принимает список или объект со списком."""
    if isinstance(body, Mapping):
        for key in ("matches", "results", "chunks", "items", "sources"):
            if key in body:
                return parse_chunks(body[key])
        raise ValueError(f"no list of chunks in /faq/search response: {sorted(body)}")
    return parse_chunks(body)


def parse_chunks(items: Any) -> list[RetrievedChunk]:
    if not isinstance(items, list):
        raise ValueError(f"expected a list of chunks, got {type(items).__name__}")
    return [_parse_chunk(item) for item in items]


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_chunk(item: Mapping[str, Any]) -> RetrievedChunk:
    material_id = _first(item, "material_id")
    position = _first(item, "position")
    chunk_id = _first(item, "chunk_id", "id") or f"{material_id}:{position}"
    material = (
        _first(item, "material_title", "title", "filename", "material") or material_id
    )
    heading_path = item.get("heading_path") or []
    if isinstance(heading_path, str):
        heading_path = [
            part.strip() for part in heading_path.split(">") if part.strip()
        ]
    distance = item.get("distance")
    return RetrievedChunk(
        id=str(chunk_id),
        material=str(material or ""),
        content=str(_first(item, "llm_text", "content", "text") or ""),
        heading_path=[str(part) for part in heading_path],
        distance=float(distance) if distance is not None else None,
    )


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()
