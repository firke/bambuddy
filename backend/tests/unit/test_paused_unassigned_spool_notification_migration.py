"""Migration test for notification_providers.on_print_paused_unassigned_spool.

The column gates the push that fires when pause_print_on_unassigned_spool halts
a print. Unlike its warn-only sibling on_print_missing_spool_assignment (default
0), it must default to *1*: a paused printer is sitting idle waiting on the
user, so existing providers opt in automatically rather than silently missing
the one notification that needs acting on. A 0 or NULL backfill would leave
upgraded installs pausing prints with no phone alert at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations

LEGACY_NOTIFICATION_PROVIDERS = """
CREATE TABLE notification_providers (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    provider_type VARCHAR(50) NOT NULL,
    enabled BOOLEAN DEFAULT 1,
    config TEXT NOT NULL,
    on_print_start BOOLEAN DEFAULT 0,
    on_print_complete BOOLEAN DEFAULT 1,
    on_print_failed BOOLEAN DEFAULT 1,
    on_print_missing_spool_assignment BOOLEAN DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """settings.database_url may point at Postgres in dev configs; the test engine
    is SQLite, so force the dialect both places run_migrations reads it from."""
    from backend.app.core import database as database_module, db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


@pytest.fixture
async def legacy_engine():
    """A modern schema with a pre-feature notification_providers table + one provider."""
    from backend.app.core.database import Base
    from backend.app.models import (  # noqa: F401
        ams_history,
        ams_label,
        api_key,
        archive,
        color_catalog,
        external_link,
        filament,
        group,
        kprofile_note,
        maintenance,
        notification,
        notification_template,
        print_log,
        print_queue,
        printer,
        project,
        project_bom,
        settings,
        slot_preset,
        smart_plug,
        smart_plug_energy_snapshot,
        spool,
        spool_assignment,
        spool_catalog,
        spool_k_profile,
        spool_usage_history,
        spoolbuddy_device,
        user,
        user_email_pref,
        virtual_printer,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE notification_providers"))
        await conn.execute(text(LEGACY_NOTIFICATION_PROVIDERS))
        await conn.execute(
            text(
                "INSERT INTO notification_providers (id, name, provider_type, config) "
                "VALUES (1, 'My Phone', 'pushover', '{}')"
            )
        )
    yield engine
    await engine.dispose()


async def test_column_missing_before_migration(legacy_engine):
    """Sanity check so the assertion below can't pass by accident."""
    async with legacy_engine.begin() as conn:
        columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(notification_providers)"))}
    assert "on_print_paused_unassigned_spool" not in columns


async def test_existing_providers_opt_in_by_default(legacy_engine):
    """An upgraded install gets the paused push without touching settings."""
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)

        columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(notification_providers)"))}
        assert "on_print_paused_unassigned_spool" in columns

        value = (
            await conn.execute(text("SELECT on_print_paused_unassigned_spool FROM notification_providers WHERE id = 1"))
        ).scalar()
        assert value == 1

        # The warn-only sibling must stay opt-out — the two are independent.
        warn_value = (
            await conn.execute(
                text("SELECT on_print_missing_spool_assignment FROM notification_providers WHERE id = 1")
            )
        ).scalar()
        assert warn_value == 0


async def test_migration_is_idempotent(legacy_engine):
    """run_migrations runs on every boot; a second pass must not error."""
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)
    async with legacy_engine.begin() as conn:
        await run_migrations(conn)

        value = (
            await conn.execute(text("SELECT on_print_paused_unassigned_spool FROM notification_providers WHERE id = 1"))
        ).scalar()
        assert value == 1
