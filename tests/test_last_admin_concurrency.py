"""Real PostgreSQL races across last-administrator removal paths.

Use the normal isolated test engine in CI. Local subset runs may supply an
engine fixture with only tenants/accounts/departments/users in a fresh schema.
Only the audit writer is mocked; services, repositories and locks are real.
"""

import asyncio
from contextlib import suppress
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from corp_ed.core.exceptions import LastAdminError, NotFoundError, PermissionError
from corp_ed.core.tenant_context import current_tenant, tenant_scope
from corp_ed.domain.models import Account, MemberStatus, Tenant, User, UserRole
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.account_service import AccountService, erase_account
from corp_ed.services.user_service import UserService


@pytest.fixture
async def guard_data(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tenant_ids = sorted([uuid4(), uuid4()])
    account_ids = [uuid4(), uuid4()]
    member_ids = {}
    async with maker() as session:
        session.add_all(
            Tenant(id=tid, name="Guard test", company_code=f"guard-{tid.hex}", seats=20)
            for tid in tenant_ids
        )
        session.add_all(
            Account(id=aid, email=f"{aid.hex}@example.test", hashed_password="unused")
            for aid in account_ids
        )
        await session.commit()
        # Opposite membership creation order, to exercise erase's lock order.
        for index, aid in enumerate(account_ids):
            order = tenant_ids if index == 0 else list(reversed(tenant_ids))
            for tid in order:
                with tenant_scope(tid):
                    member = User(
                        tenant_id=tid,
                        account_id=aid,
                        role=UserRole.ADMIN,
                        status=MemberStatus.ACTIVE,
                    )
                    session.add(member)
                    await session.flush()
                    member_ids[aid, tid] = member.id
            await session.commit()
    return maker, tenant_ids, account_ids, member_ids


async def perform(session, actor, target_id, operation):
    audit = Mock()
    service = UserService(UserRepository(session), audit, session)
    if operation == "demote":
        await service.update_user(actor, target_id, role=UserRole.EMPLOYEE)
    elif operation == "block":
        await service.update_user(actor, target_id, blocked=True)
    elif operation == "remove":
        await service.remove(actor, target_id)
    elif operation == "leave":
        account = await AccountRepository(session).get(actor.account_id)
        await AccountService(session, Mock(), audit, Mock()).leave_company(
            account, actor.tenant_id
        )
    else:
        assert operation == "erase"
        account = await AccountRepository(session).get(actor.account_id)
        await erase_account(session, audit, account)


@pytest.mark.parametrize(
    ("first_operation", "second_operation"),
    [
        ("demote", "demote"),
        ("block", "remove"),
        ("remove", "block"),
        ("leave", "leave"),
        ("erase", "leave"),
        ("leave", "erase"),
        ("erase", "erase"),
    ],
)
async def test_concurrent_removals_keep_an_active_admin(
    guard_data, monkeypatch, first_operation, second_operation
):
    maker, tenants, accounts, members = guard_data
    tid = tenants[0]
    counted = asyncio.Event()
    other_counted = asyncio.Event()
    release_first = asyncio.Event()
    original_count = UserRepository.count_active_admins
    async with maker() as first, maker() as second:
        with tenant_scope(tid):
            first_actor = await UserRepository(first).get_by_id(
                members[accounts[0], tid]
            )
            second_actor = await UserRepository(second).get_by_id(
                members[accounts[1], tid]
            )
            # Preload targets too: select alone must not reuse stale role/status.
            await UserRepository(first).get_by_id(second_actor.id)
            await UserRepository(second).get_by_id(first_actor.id)

            async def held_count(repo):
                result = await original_count(repo)
                at_final_check = (
                    first_operation != "erase" or current_tenant.get() == tenants[-1]
                )
                if repo.session is first and at_final_check and not counted.is_set():
                    counted.set()
                    await release_first.wait()
                elif repo.session is second:
                    other_counted.set()
                return result

            monkeypatch.setattr(UserRepository, "count_active_admins", held_count)

            async def invoke(session, actor, target, operation):
                try:
                    await perform(session, actor, target, operation)
                except (LastAdminError, PermissionError) as exc:
                    await session.rollback()
                    return exc
                return None

            one = asyncio.create_task(
                invoke(first, first_actor, second_actor.id, first_operation)
            )
            await asyncio.wait_for(counted.wait(), timeout=5)
            two = asyncio.create_task(
                invoke(second, second_actor, first_actor.id, second_operation)
            )
            try:
                # Without serialization the second operation reads the same
                # count of two. With it, the second waits at the tenant lock.
                with suppress(TimeoutError):
                    await asyncio.wait_for(other_counted.wait(), timeout=0.2)
            finally:
                release_first.set()
            results = await asyncio.wait_for(asyncio.gather(one, two), timeout=5)

    async with maker() as check:
        for tenant_id in tenants:
            with tenant_scope(tenant_id):
                assert await original_count(UserRepository(check)) >= 1
    assert sum(result is None for result in results) == 1
    if first_operation in {"demote", "block", "remove"}:
        # A stale HTTP-authorized actor cannot mutate the remaining admin.
        assert isinstance(results[1], PermissionError)
    else:
        assert isinstance(results[1], LastAdminError)


async def test_account_erasure_locks_companies_in_stable_order(guard_data, monkeypatch):
    maker, tenants, accounts, members = guard_data
    locked = []
    original_lock = TenantRepository.lock

    async def record_lock(repo, tenant_id):
        locked.append(tenant_id)
        return await original_lock(repo, tenant_id)

    monkeypatch.setattr(TenantRepository, "lock", record_lock)
    async with maker() as session:
        account = await AccountRepository(session).get(accounts[1])
        await erase_account(session, Mock(), account)
    assert locked == tenants


@pytest.mark.parametrize("operation", ["demote", "block", "remove"])
async def test_stale_admin_cannot_edit_even_a_non_admin(guard_data, operation):
    maker, tenants, accounts, members = guard_data
    tid = tenants[0]
    async with maker() as old_request, maker() as revoke:
        with tenant_scope(tid):
            stale = await UserRepository(old_request).get_by_id(
                members[accounts[0], tid]
            )
            fresh = await UserRepository(revoke).get_by_id(stale.id)
            fresh.role = UserRole.EMPLOYEE
            employee = User(
                tenant_id=tid, role=UserRole.EMPLOYEE, status=MemberStatus.ACTIVE
            )
            revoke.add(employee)
            await revoke.commit()
            # The old request's identity map still has admin=True.
            assert stale.role is UserRole.ADMIN
            with pytest.raises(PermissionError):
                await perform(old_request, stale, employee.id, operation)
            await old_request.rollback()


@pytest.mark.parametrize("case", ["unknown", "nonmember", "left"])
async def test_leave_requires_membership_before_locking_company(
    guard_data, monkeypatch, case
):
    maker, tenants, accounts, members = guard_data
    tenant_id = uuid4()
    async with maker() as session:
        account = await AccountRepository(session).get(accounts[0])
        if case == "nonmember":
            session.add(
                Tenant(
                    id=tenant_id,
                    name="Other",
                    company_code=f"other-{tenant_id.hex}",
                    seats=20,
                )
            )
            await session.commit()
        elif case == "left":
            tenant_id = tenants[0]
            with tenant_scope(tenant_id):
                member = await UserRepository(session).get_by_id(
                    members[accounts[0], tenant_id]
                )
                member.status = MemberStatus.LEFT
                await session.commit()

        async def forbidden_lock(*args):
            pytest.fail("Nonmembers must not lock an arbitrary company")

        monkeypatch.setattr(TenantRepository, "lock", forbidden_lock)
        with pytest.raises(NotFoundError):
            await AccountService(session, Mock(), Mock(), Mock()).leave_company(
                account, tenant_id
            )


@pytest.mark.parametrize("operation", ["leave", "erase"])
async def test_last_active_admin_cannot_leave_or_erase_account(guard_data, operation):
    maker, tenants, accounts, members = guard_data
    async with maker() as session:
        for tenant_id in tenants:
            with tenant_scope(tenant_id):
                other = await UserRepository(session).get_by_id(
                    members[accounts[1], tenant_id]
                )
                other.role = UserRole.EMPLOYEE
                await session.flush()
        await session.commit()
        with tenant_scope(tenants[0]):
            actor = await UserRepository(session).get_by_id(
                members[accounts[0], tenants[0]]
            )
            with pytest.raises(LastAdminError):
                await perform(
                    session, actor, members[accounts[1], tenants[0]], operation
                )
            await session.rollback()


@pytest.mark.parametrize("operation", ["leave", "erase"])
async def test_self_removal_uses_account_then_tenant_lock_order(
    guard_data, monkeypatch, operation
):
    maker, tenants, accounts, members = guard_data
    attempted = asyncio.Event()
    original_account_lock = AccountRepository.get_for_update
    original_tenant_lock = TenantRepository.lock
    async with maker() as joining, maker() as removing:
        # Invite.accept's account terms autoflush acquires this row first.
        await original_account_lock(AccountRepository(joining), accounts[0])
        with tenant_scope(tenants[0]):
            actor = await UserRepository(removing).get_by_id(
                members[accounts[0], tenants[0]]
            )

            async def account_lock(repo, account_id):
                if repo.session is removing:
                    attempted.set()
                return await original_account_lock(repo, account_id)

            async def tenant_lock(repo, tenant_id):
                if repo.session is removing:
                    attempted.set()
                return await original_tenant_lock(repo, tenant_id)

            monkeypatch.setattr(AccountRepository, "get_for_update", account_lock)
            monkeypatch.setattr(TenantRepository, "lock", tenant_lock)
            task = asyncio.create_task(perform(removing, actor, actor.id, operation))
            try:
                await asyncio.wait_for(attempted.wait(), timeout=5)
                # The account holder must still be able to acquire the tenant.
                # Tenant→Account in the removal would deadlock these requests.
                await asyncio.wait_for(
                    original_tenant_lock(TenantRepository(joining), tenants[0]),
                    timeout=2,
                )
            finally:
                await joining.rollback()
                await asyncio.wait_for(task, timeout=5)
