"""Add core models: tenant, user, brief, program, material

Revision ID: e33fe01cbf3a
Revises:
Create Date: 2026-06-15 16:20:58.267777

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e33fe01cbf3a'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # создаём ENUM-типы один раз через postgresql.ENUM (create_type здесь работает)
    track = postgresql.ENUM('MARKETING', 'ANALYTICS', name='track')
    userrole = postgresql.ENUM('MANAGER', 'INTERN', name='userrole')
    programstatus = postgresql.ENUM('DRAFT', 'APPROVED', name='programstatus')
    track.create(op.get_bind(), checkfirst=True)
    userrole.create(op.get_bind(), checkfirst=True)
    programstatus.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'materials',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('track', postgresql.ENUM('MARKETING', 'ANALYTICS', name='track', create_type=False), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_materials_tenant_id'), 'materials', ['tenant_id'], unique=False)

    op.create_table(
        'tenants',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('company_code', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_tenants_company_code'), 'tenants', ['company_code'], unique=True)

    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('hashed_password', sa.String(), nullable=False),
        sa.Column('full_name', sa.String(), nullable=True),
        sa.Column('role', postgresql.ENUM('MANAGER', 'INTERN', name='userrole', create_type=False), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'email', name='uq_user_tenant_email'),
    )
    op.create_index(op.f('ix_users_tenant_id'), 'users', ['tenant_id'], unique=False)

    op.create_table(
        'briefs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('author_id', sa.Uuid(), nullable=False),
        sa.Column('track', postgresql.ENUM('MARKETING', 'ANALYTICS', name='track', create_type=False), nullable=False),
        sa.Column('role_title', sa.String(), nullable=False),
        sa.Column('goals', sa.String(), nullable=False),
        sa.Column('tasks', sa.String(), nullable=False),
        sa.Column('intern_level', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_briefs_tenant_id'), 'briefs', ['tenant_id'], unique=False)

    op.create_table(
        'programs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('brief_id', sa.Uuid(), nullable=False),
        sa.Column('intern_id', sa.Uuid(), nullable=True),
        sa.Column('status', postgresql.ENUM('DRAFT', 'APPROVED', name='programstatus', create_type=False), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['brief_id'], ['briefs.id']),
        sa.ForeignKeyConstraint(['intern_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_programs_tenant_id'), 'programs', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_programs_tenant_id'), table_name='programs')
    op.drop_table('programs')
    op.drop_index(op.f('ix_briefs_tenant_id'), table_name='briefs')
    op.drop_table('briefs')
    op.drop_index(op.f('ix_users_tenant_id'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_tenants_company_code'), table_name='tenants')
    op.drop_table('tenants')
    op.drop_index(op.f('ix_materials_tenant_id'), table_name='materials')
    op.drop_table('materials')
    postgresql.ENUM(name='track').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='userrole').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='programstatus').drop(op.get_bind(), checkfirst=True)
