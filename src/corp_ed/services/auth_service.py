from corp_ed.core.exceptions import InvalidCredentialsError
from corp_ed.core.security import create_access_token, verify_password
from corp_ed.core.tenant_context import current_tenant
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository


class AuthService:
    def __init__(
        self,
        tenant_repo: TenantRepository,
        user_repo: UserRepository,
    ) -> None:
        self.tenant_repo = tenant_repo
        self.user_repo = user_repo

    async def login(self, company_code: str, email: str, password: str) -> str:
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        if tenant is None:
            raise InvalidCredentialsError()

        current_tenant.set(tenant.id)

        user = await self.user_repo.get_by_email(email)
        if user is None:
            raise InvalidCredentialsError()

        if not verify_password(password, user.hashed_password):
            raise InvalidCredentialsError()

        return create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role.value,
        )
