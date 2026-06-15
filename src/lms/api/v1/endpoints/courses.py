from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from lms.api.v1.schemas.course import CourseCreate, CourseResponse
from lms.core.database import get_session
from lms.repositories.course_repository import CourseRepository

router = APIRouter(prefix="/courses", tags=["courses"])


def get_course_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CourseRepository:
    return CourseRepository(session)


@router.post("", response_model=CourseResponse, status_code=201)
async def create_course(
    data: CourseCreate,
    repo: Annotated[CourseRepository, Depends(get_course_repository)],
) -> CourseResponse:
    course = await repo.create(title=data.title)
    return CourseResponse.model_validate(course)


@router.get("", response_model=list[CourseResponse])
async def list_courses(
    repo: Annotated[CourseRepository, Depends(get_course_repository)],
) -> Sequence[CourseResponse]:
    courses = await repo.list_all()
    return [CourseResponse.model_validate(c) for c in courses]
