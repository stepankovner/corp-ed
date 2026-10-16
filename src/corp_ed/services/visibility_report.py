"""Кто что видит в подключении: отчёт для проверки прав на настоящем стенде.

Для каждого подключения компании: сколько материалов, какие шифры
документов в них найдены и какие из шифров видит каждый активный участник
компании. Видимость считается тем же правилом ядра, что и поиск ассистента
(`ChunkRepository.visible_material_ids`), а не по сырым записям доступа.

Шифр документа ищется в тексте материала регулярным выражением (группа 1 —
шифр). Шаблон по умолчанию — как в проверке приёмки (tests/acceptance);
для другого корпуса шаблон задают снаружи. В отчёте нет ни текста
материалов, ни учётных данных: только шифры, почты участников и счётчики.
"""

import re
from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Material, MemberStatus, Tenant, User
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.connector_repository import ConnectorRepository

# Тот же шаблон, что CODE в tests/acceptance/scenario.py; тест следит,
# чтобы они не разошлись.
DEFAULT_CODE_PATTERN = r"Шифр документа[:\s|*]*(SV-[A-Z]+-\d{3})"

# Сколько материалов отдаём правилу видимости за один запрос.
_BATCH = 500


class VisibilityReportError(ValueError):
    """Неверные параметры отчёта (шаблон, подключение)."""


def compile_code_pattern(pattern: str) -> re.Pattern[str]:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise VisibilityReportError(f"неверный шаблон шифра: {exc}") from exc
    if compiled.groups < 1:
        raise VisibilityReportError("в шаблоне шифра нужна группа (...) с шифром")
    return compiled


def _batches(ids: Sequence[UUID]) -> Iterable[Sequence[UUID]]:
    for start in range(0, len(ids), _BATCH):
        yield ids[start : start + _BATCH]


async def _active_members(session: AsyncSession) -> dict[str, UUID]:
    """почта → id членства активных участников компании из контекста."""
    members = await session.scalars(
        select(User).where(User.status == MemberStatus.ACTIVE)
    )
    return {
        user.email.casefold(): user.id for user in members if user.email is not None
    }


async def connector_visibility(
    session: AsyncSession,
    tenant: Tenant,
    *,
    connector_id: UUID | None = None,
    code_pattern: str = DEFAULT_CODE_PATTERN,
) -> dict[str, Any]:
    pattern = compile_code_pattern(code_pattern)
    connectors_report: list[dict[str, Any]] = []
    with tenant_scope(tenant.id):
        connectors = await ConnectorRepository(session).list_all()
        if connector_id is not None:
            connectors = [c for c in connectors if c.id == connector_id]
            if not connectors:
                raise VisibilityReportError("у компании нет такого подключения")
        members = await _active_members(session)
        chunks = ChunkRepository(session)
        for connector in connectors:
            code_by_material: dict[UUID, str] = {}
            without_code = 0
            total = 0
            rows = await session.stream(
                # Колоночный select: хук изоляции его не видит, фильтр явный.
                select(Material.id, Material.content).where(
                    Material.tenant_id == tenant.id,
                    Material.connector_id == connector.id,
                )
            )
            async for material_id, content in rows:
                total += 1
                found = pattern.search(content or "")
                if found is None:
                    without_code += 1
                else:
                    code_by_material[material_id] = found.group(1)
            documents: dict[str, int] = {}
            for code in code_by_material.values():
                documents[code] = documents.get(code, 0) + 1
            ids = list(code_by_material)
            seen: dict[str, list[str]] = {}
            for email in sorted(members):
                visible: set[UUID] = set()
                for batch in _batches(ids):
                    visible |= await chunks.visible_material_ids(
                        batch, viewer=members[email]
                    )
                seen[email] = sorted({code_by_material[i] for i in visible})
            connectors_report.append(
                {
                    "id": str(connector.id),
                    "kind": connector.kind,
                    "name": connector.name,
                    "materials": total,
                    "documents": dict(sorted(documents.items())),
                    "without_code": without_code,
                    "seen": seen,
                }
            )
    return {"company": tenant.company_code, "connectors": connectors_report}
