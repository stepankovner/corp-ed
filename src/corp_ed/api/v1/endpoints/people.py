"""Справочник коллег (ТЗ §4): люди своей компании и их контакты."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_current_user, get_people_service
from corp_ed.api.v1.rate_limits import PEOPLE_EDIT_PER_TENANT, limit_by_tenant
from corp_ed.api.v1.schemas.auth import DepartmentRef
from corp_ed.api.v1.schemas.people import PersonResponse, PersonUpdateRequest
from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import User
from corp_ed.services.people_service import PeopleService, Person

router = APIRouter(prefix="/people", tags=["people"])

Member = Annotated[User, Depends(get_current_user)]
Service = Annotated[PeopleService, Depends(get_people_service)]


def person_response(person: Person) -> PersonResponse:
    member = person.member
    account = member.account
    if account is None:
        # Удалившие учётку в справочник не попадают (list_people, get_person).
        raise NotFoundError("Сотрудник не найден")
    return PersonResponse(
        member_id=member.id,
        first_name=account.first_name,
        last_name=account.last_name,
        patronymic=account.patronymic,
        full_name=account.full_name,
        email=account.email,
        phone=account.phone,
        telegram=account.telegram,
        avatar_url=person.avatar_url,
        position=member.position,
        department=DepartmentRef(id=person.department.id, name=person.department.name)
        if person.department
        else None,
        role=member.role,
    )


@router.get("", response_model=list[PersonResponse])
async def list_people(service: Service, current_user: Member) -> list[PersonResponse]:
    """Работающие люди компании по алфавиту. Видят только её люди."""
    return [person_response(person) for person in await service.list_people()]


@router.get("/{member_id}", response_model=PersonResponse)
async def read_person(
    member_id: UUID, service: Service, current_user: Member
) -> PersonResponse:
    return person_response(await service.get_person(member_id))


@router.patch(
    "/{member_id}",
    response_model=PersonResponse,
    dependencies=[Depends(limit_by_tenant(PEOPLE_EDIT_PER_TENANT))],
)
async def update_person(
    member_id: UUID,
    data: PersonUpdateRequest,
    service: Service,
    current_user: Member,
) -> PersonResponse:
    """Должность и отдел: свои — сам человек, чужие — администратор."""
    changes = data.model_dump(exclude_unset=True)
    if "position" in changes:
        changes["position"] = changes["position"] or None
    person = await service.update_work(current_user, member_id, **changes)
    return person_response(person)
