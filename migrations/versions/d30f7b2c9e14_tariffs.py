"""tariffs

Revision ID: d30f7b2c9e14
Revises: c28d1a0e4b51
Create Date: 2026-09-30 15:00:00.000000

Тарифы (решение Артёма 30.09, domain/tariffs.py): «Базовый» (до 5
подключений), «Расширенный» (без тарифного лимита), «Корпоративный».
Отменяет «число подключений не тарифицируется» от 25.09. Заведённые
компании получают «Базовый»; команда переводит их `cli set-tariff`.
connector_limit — технический потолок подключений для одной компании
(NULL — общий CONNECTOR_MAX_PER_TENANT). Заявки: «custom» (тарифы выше,
«по запросу») становится «enterprise».
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd30f7b2c9e14'
down_revision: Union[str, Sequence[str], None] = 'c28d1a0e4b51'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'tenants',
        sa.Column('tariff', sa.String(length=16), server_default='base', nullable=False),
    )
    op.add_column('tenants', sa.Column('connector_limit', sa.Integer(), nullable=True))
    op.create_check_constraint(
        'ck_tenants_tariff', 'tenants', "tariff IN ('base', 'extended', 'enterprise')"
    )
    op.create_check_constraint(
        'ck_tenants_connector_limit_positive',
        'tenants',
        'connector_limit IS NULL OR connector_limit > 0',
    )
    op.drop_constraint('ck_leads_tariff', 'leads', type_='check')
    op.execute("UPDATE leads SET tariff = 'enterprise' WHERE tariff = 'custom'")
    op.create_check_constraint(
        'ck_leads_tariff', 'leads', "tariff IN ('base', 'extended', 'enterprise')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_leads_tariff', 'leads', type_='check')
    op.execute("UPDATE leads SET tariff = 'custom' WHERE tariff <> 'base'")
    op.create_check_constraint('ck_leads_tariff', 'leads', "tariff IN ('base', 'custom')")
    op.drop_constraint('ck_tenants_connector_limit_positive', 'tenants', type_='check')
    op.drop_constraint('ck_tenants_tariff', 'tenants', type_='check')
    op.drop_column('tenants', 'connector_limit')
    op.drop_column('tenants', 'tariff')
