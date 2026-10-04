"""Фабрики тестовых данных.

make_user принимает поля «пользователя» до 03.10 (почта, пароль, имя,
is_active, must_change_password) и раскладывает их в учётку и членство
(ТЗ §2): старые тесты переводятся без переписывания каждого сценария.
Учётка добавляется в сессию каскадом вместе с членством.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from corp_ed.domain.models import Account, MemberStatus, User, UserRole


def make_account(
    email: str,
    *,
    hashed_password: str = "hashed",
    full_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    verified: bool = True,
    must_change_password: bool = False,
) -> Account:
    if full_name is not None and first_name is None and last_name is None:
        first, _, last = full_name.strip().partition(" ")
        first_name, last_name = first or None, last.strip() or None
    return Account(
        email=email.strip().casefold(),
        hashed_password=hashed_password,
        first_name=first_name,
        last_name=last_name,
        email_verified_at=datetime.now(UTC) if verified else None,
        must_change_password=must_change_password,
    )


def make_user(
    *,
    email: str,
    role: UserRole = UserRole.EMPLOYEE,
    tenant_id: UUID | None = None,
    hashed_password: str = "hashed",
    full_name: str | None = None,
    is_active: bool = True,
    must_change_password: bool = False,
    account: Account | None = None,
    status: MemberStatus | None = None,
    **fields: Any,
) -> User:
    if account is None:
        account = make_account(
            email,
            hashed_password=hashed_password,
            full_name=full_name,
            must_change_password=must_change_password,
        )
    if status is None:
        status = MemberStatus.ACTIVE if is_active else MemberStatus.BLOCKED
    if tenant_id is not None:
        fields["tenant_id"] = tenant_id
    return User(account=account, role=role, status=status, **fields)
