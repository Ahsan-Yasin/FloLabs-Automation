"""initial schema: users, email tokens, refresh tokens, API keys, jobs index,
webhook deliveries (plan.MD section 4)

Revision ID: 0001
Revises:
Create Date: 2026-09-27 14:31:07.903196
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('email_verified_at', sa.DateTime(), nullable=True),
    sa.Column('zoom_host_email', sa.String(length=320), nullable=True),
    sa.Column('webhook_secret', sa.String(length=128), nullable=False),
    sa.Column('notify_on_done', sa.Boolean(), nullable=False),
    sa.Column('tokens_valid_after', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('url', sa.String(length=2048), nullable=False),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('delivered_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries'))
    )
    with op.batch_alter_table('webhook_deliveries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_webhook_deliveries_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_webhook_deliveries_next_attempt_at'), ['next_attempt_at'], unique=False)

    op.create_table('api_keys',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('scopes_json', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_keys_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys')),
    sa.UniqueConstraint('key_hash', name=op.f('uq_api_keys_key_hash')),
    sa.UniqueConstraint('prefix', name=op.f('uq_api_keys_prefix'))
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_keys_user_id'), ['user_id'], unique=False)

    op.create_table('email_tokens',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('used_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_email_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_email_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_email_tokens_token_hash'))
    )
    with op.batch_alter_table('email_tokens', schema=None) as batch_op:
        batch_op.create_index('ix_email_tokens_user_kind', ['user_id', 'kind'], unique=False)

    op.create_table('jobs_index',
    sa.Column('job_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('source_kind', sa.String(length=16), nullable=False),
    sa.Column('created_via', sa.String(length=16), nullable=False),
    sa.Column('api_key_id', sa.String(length=32), nullable=True),
    sa.Column('callback_url', sa.String(length=2048), nullable=True),
    sa.Column('source_duration_s', sa.Float(), nullable=True),
    sa.Column('bundle_bytes', sa.Integer(), nullable=True),
    sa.Column('error_code', sa.String(length=48), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_jobs_index_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('job_id', name=op.f('pk_jobs_index'))
    )
    with op.batch_alter_table('jobs_index', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_jobs_index_status'), ['status'], unique=False)
        batch_op.create_index('ix_jobs_index_user_created', ['user_id', 'created_at'], unique=False)

    op.create_table('refresh_tokens',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('family_id', sa.String(length=32), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('replaced_by_id', sa.String(length=32), nullable=True),
    sa.Column('replaced_at', sa.DateTime(), nullable=True),
    sa.Column('user_agent', sa.String(length=300), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_refresh_tokens_family_id'), ['family_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_refresh_tokens_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_refresh_tokens_user_id'))
        batch_op.drop_index(batch_op.f('ix_refresh_tokens_family_id'))

    op.drop_table('refresh_tokens')
    with op.batch_alter_table('jobs_index', schema=None) as batch_op:
        batch_op.drop_index('ix_jobs_index_user_created')
        batch_op.drop_index(batch_op.f('ix_jobs_index_status'))

    op.drop_table('jobs_index')
    with op.batch_alter_table('email_tokens', schema=None) as batch_op:
        batch_op.drop_index('ix_email_tokens_user_kind')

    op.drop_table('email_tokens')
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_keys_user_id'))

    op.drop_table('api_keys')
    with op.batch_alter_table('webhook_deliveries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webhook_deliveries_next_attempt_at'))
        batch_op.drop_index(batch_op.f('ix_webhook_deliveries_job_id'))

    op.drop_table('webhook_deliveries')
    op.drop_table('users')
