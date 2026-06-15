# src/lms/domain/mixins.py
from uuid import UUID

from sqlalchemy.orm import Mapped, mapped_column


class TenantMixin:
    tenant_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
