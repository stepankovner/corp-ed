"""Ручки агента сетевых папок (docs/SMB-AGENT.md §3).

Не для браузера: вход — ключом агента в заголовке Authorization, а не
сессией сотрудника. Ключ даёт доступ только к своему подключению.
"""

import contextlib
import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import get_adapter_registry, get_secret_box
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import get_connector_settings
from corp_ed.core.database import get_session
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.ingest.extract import ExtractionError, max_file_bytes
from corp_ed.ingest.sandbox import extract_isolated
from corp_ed.services.agent_ingest_service import (
    MAX_ACCESS_BATCH,
    MAX_EMAILS_PER_DOCUMENT,
    AgentDocument,
    AgentIngestService,
    AgentTokenError,
    ConnectorStoppedError,
    RunNotFoundError,
    authenticate,
    parse_token,
)

# Протокол агента описан в docs/SMB-AGENT.md; в схему API фронта не входит.
router = APIRouter(prefix="/agent", tags=["agent"], include_in_schema=False)

Emails = Annotated[
    list[Annotated[str, Field(max_length=254)]],
    Field(max_length=MAX_EMAILS_PER_DOCUMENT),
]


class DocumentMeta(BaseModel):
    run_id: UUID
    # Как materials.external_id; длинный путь агент заменяет хешем.
    external_id: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    # Имя файла без расширения бывает длиннее заголовка материала (200) —
    # обрезается при сохранении, а не отвергается.
    title: str = Field(min_length=1, max_length=1024)
    path: str = Field(max_length=32768)
    filename: str = Field(min_length=1, max_length=255)
    modified_at: datetime | None = None
    allowed_emails: Emails


class AccessItem(BaseModel):
    external_id: str = Field(min_length=1, max_length=512)
    allowed_emails: Emails


class AccessRequest(BaseModel):
    run_id: UUID
    documents: list[AccessItem] = Field(max_length=MAX_ACCESS_BATCH)


class FinishRequest(BaseModel):
    complete: bool
    stats: dict[str, Any] = Field(default_factory=dict)


class StateItem(BaseModel):
    external_id: str
    version: str


class StateResponse(BaseModel):
    documents: list[StateItem]
    next: str | None


class _Deps:
    """Сессия, ключи и реестр; service() — подключение по ключу агента."""

    def __init__(
        self,
        session: Annotated[AsyncSession, Depends(get_session)],
        box: Annotated[SecretBox, Depends(get_secret_box)],
        registry: Annotated[AdapterRegistry, Depends(get_adapter_registry)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        self.session = session
        self.box = box
        self.registry = registry
        self.authorization = authorization

    async def service(self) -> AgentIngestService:
        raw = self.authorization or ""
        if not raw.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="agent_token_required")
        try:
            token = parse_token(raw.removeprefix("Bearer ").strip())
            with tenant_scope(token.tenant_id):
                connector = await authenticate(
                    self.session, token, self.box, self.registry.push_kinds()
                )
        except AgentTokenError as exc:
            raise HTTPException(status_code=401, detail="agent_token_invalid") from exc
        except ConnectorStoppedError as exc:
            raise HTTPException(status_code=403, detail="connector_stopped") from exc
        return AgentIngestService(
            self.session,
            connector,
            get_connector_settings(),
            extractor=extract_isolated,
        )


Deps = Annotated[_Deps, Depends()]


@router.get("/state", response_model=StateResponse)
async def state(deps: Deps, after: str | None = None, limit: int = 1000) -> Any:
    service = await deps.service()
    with tenant_scope(service.tenant_id):
        rows, following = await service.state(after=after, limit=limit)
    return StateResponse(
        documents=[StateItem(external_id=e, version=v) for e, v in rows],
        next=following,
    )


@router.post("/runs")
async def start_run(deps: Deps) -> dict[str, str]:
    service = await deps.service()
    with tenant_scope(service.tenant_id):
        run = await service.start_run()
    return {"run_id": str(run.id), "started_at": run.started_at.isoformat()}


@router.put("/documents")
async def put_document(
    deps: Deps,
    meta: Annotated[str, Form(max_length=1_000_000)],
    file: Annotated[UploadFile, File()],
) -> dict[str, str]:
    try:
        parsed = DocumentMeta.model_validate(json.loads(meta))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="bad_meta") from exc
    service = await deps.service()
    settings = get_connector_settings()
    limit = max_file_bytes(
        parsed.filename,
        large=settings.max_large_document_bytes,
        other=settings.max_document_bytes,
    )
    data = await file.read(limit + 1)
    with tenant_scope(service.tenant_id):
        if len(data) > limit:
            await _count_failure(service, parsed.run_id)
            raise HTTPException(status_code=413, detail="document_too_large")
        try:
            material, change = await service.put_document(
                parsed.run_id,
                AgentDocument(
                    external_id=parsed.external_id,
                    version=parsed.version,
                    title=parsed.title,
                    path=parsed.path,
                    filename=parsed.filename,
                    modified_at=parsed.modified_at,
                    allowed_emails=parsed.allowed_emails,
                ),
                data,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=409, detail="run_not_open") from exc
        except ExtractionError as exc:
            await service.session.rollback()
            await _count_failure(service, parsed.run_id)
            status = 413 if exc.code == "document_too_large" else 415
            raise HTTPException(status_code=status, detail=exc.code) from exc
    return {"status": change, "material_id": str(material.id)}


async def _count_failure(service: AgentIngestService, run_id: UUID) -> None:
    with contextlib.suppress(RunNotFoundError):
        await service.count_failure(run_id)


@router.post("/access")
async def update_access(deps: Deps, body: AccessRequest) -> dict[str, Any]:
    service = await deps.service()
    with tenant_scope(service.tenant_id):
        try:
            updated, unknown = await service.update_access(
                body.run_id,
                [(item.external_id, item.allowed_emails) for item in body.documents],
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=409, detail="run_not_open") from exc
    return {"updated": updated, "unknown": unknown}


@router.post("/runs/{run_id}/finish")
async def finish_run(deps: Deps, run_id: UUID, body: FinishRequest) -> dict[str, Any]:
    service = await deps.service()
    with tenant_scope(service.tenant_id):
        try:
            run = await service.finish(
                run_id, complete=body.complete, agent_stats=body.stats
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=409, detail="run_not_open") from exc
    return {"status": run.status, "stats": run.stats}
