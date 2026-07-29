"""Unit tests for spool assignment notification service."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.spool_assignment_notifications import check_spool_assignments_on_print_start


class _FakeAssignmentsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Fake DB session that returns legacy vs. Spoolman assignment rows based
    on which table the SELECT targets, so tests can exercise either mode."""

    def __init__(
        self,
        printer_name: str,
        legacy: list[SimpleNamespace] | None = None,
        spoolman: list[SimpleNamespace] | None = None,
    ):
        self._printer = SimpleNamespace(name=printer_name)
        self._legacy = legacy or []
        self._spoolman = spoolman or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, key):
        return self._printer

    async def execute(self, statement):
        table = statement.get_final_froms()[0].name
        if table == "spoolman_slot_assignments":
            return _FakeAssignmentsResult(self._spoolman)
        return _FakeAssignmentsResult(self._legacy)


@pytest.mark.asyncio
async def test_missing_assignment_broadcasts_websocket_event_and_push_notification():
    """When a mapped tray is unassigned, service emits websocket and notification events."""
    logger = logging.getLogger(__name__)
    data = {
        "ams_mapping": [1],
        "raw_data": {},
    }

    # Assignment exists for A1 (global tray 0), but print uses A2 (global tray 1).
    assignments = [SimpleNamespace(ams_id=0, tray_id=0)]

    with (
        patch(
            "backend.app.services.spool_assignment_notifications.async_session",
            return_value=_FakeSession("Printer A", assignments),
        ),
        patch("backend.app.services.spool_assignment_notifications.printer_manager.get_status", return_value=None),
        patch(
            "backend.app.services.spool_assignment_notifications.ws_manager.send_missing_spool_assignment",
            new_callable=AsyncMock,
        ) as mock_ws,
        patch(
            "backend.app.services.spool_assignment_notifications.notification_service.on_print_missing_spool_assignment",
            new_callable=AsyncMock,
        ) as mock_notify,
    ):
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_awaited_once()
    ws_kwargs = mock_ws.await_args.kwargs
    assert ws_kwargs["printer_id"] == 1
    assert ws_kwargs["printer_name"] == "Printer A"
    assert ws_kwargs["missing_slots"] == [{"slot": "A2", "profile": "Unknown", "color": "Unknown"}]

    mock_notify.assert_awaited_once()
    notify_kwargs = mock_notify.await_args.kwargs
    assert notify_kwargs["printer_id"] == 1
    assert notify_kwargs["printer_name"] == "Printer A"
    assert notify_kwargs["missing_slots"] == [{"slot": "A2", "profile": "Unknown", "color": "Unknown"}]


def _patches(session):
    """Common patch set: the fake session + stubbed printer state / emitters.

    Both notification events are patched — the service picks exactly one of them
    depending on whether it paused, so tests assert on which was chosen.
    """
    return (
        patch(
            "backend.app.services.spool_assignment_notifications.async_session",
            return_value=session,
        ),
        patch("backend.app.services.spool_assignment_notifications.printer_manager.get_status", return_value=None),
        patch(
            "backend.app.services.spool_assignment_notifications.ws_manager.send_missing_spool_assignment",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.app.services.spool_assignment_notifications.notification_service.on_print_missing_spool_assignment",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.app.services.spool_assignment_notifications.notification_service.on_print_paused_unassigned_spool",
            new_callable=AsyncMock,
        ),
    )


@pytest.mark.asyncio
async def test_spoolman_only_assignment_suppresses_notification():
    """#1473 — trays bound only via spoolman_slot_assignments must NOT be
    flagged missing (the legacy spool_assignment table is empty in Spoolman
    mode, so checking it alone fired a false positive on every print)."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}  # print uses A1 + A2

    # Both used trays bound via Spoolman; legacy table empty.
    session = _FakeSession(
        "Printer A",
        legacy=[],
        spoolman=[SimpleNamespace(ams_id=0, tray_id=0), SimpleNamespace(ams_id=0, tray_id=1)],
    )
    p_session, p_status, p_ws, p_notify, p_paused = _patches(session)
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused:
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_not_awaited()
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_spoolman_partial_coverage_flags_only_uncovered_tray():
    """A Spoolman assignment for A1 only, with a print using A1 + A2, flags
    A2 alone."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}

    session = _FakeSession(
        "Printer A",
        legacy=[],
        spoolman=[SimpleNamespace(ams_id=0, tray_id=0)],  # A1 only
    )
    p_session, p_status, p_ws, p_notify, p_paused = _patches(session)
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused:
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_awaited_once()
    assert mock_ws.await_args.kwargs["missing_slots"] == [{"slot": "A2", "profile": "Unknown", "color": "Unknown"}]
    mock_notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_mode_union_covers_all_used_trays():
    """A1 bound in the legacy table, A2 bound in spoolman_slot_assignments —
    the union covers both used trays, so no notification fires."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}

    session = _FakeSession(
        "Printer A",
        legacy=[SimpleNamespace(ams_id=0, tray_id=0)],  # A1
        spoolman=[SimpleNamespace(ams_id=0, tray_id=1)],  # A2
    )
    p_session, p_status, p_ws, p_notify, p_paused = _patches(session)
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused:
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_not_awaited()
    mock_notify.assert_not_awaited()


# --- pause_print_on_unassigned_spool ------------------------------------------------


def _pause_patches(session, setting: str | None, client):
    """_patches plus stubs for the setting lookup and the MQTT client."""
    return (
        *_patches(session),
        patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            return_value=setting,
        ),
        patch("backend.app.services.spool_assignment_notifications.printer_manager.get_client", return_value=client),
    )


def _missing_a2_session():
    """Print uses A1 + A2; only A1 is assigned, so A2 is flagged."""
    return _FakeSession("Printer A", legacy=[SimpleNamespace(ams_id=0, tray_id=0)])


@pytest.mark.asyncio
async def test_pause_disabled_by_default_leaves_print_running():
    """With the setting unset, the missing assignment only warns — no pause."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}
    client = SimpleNamespace(pause_print=lambda: True)

    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(
        _missing_a2_session(), None, client
    )
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused as mock_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_awaited_once()
    assert mock_ws.await_args.kwargs["paused"] is False
    # Warn-only event; the paused event must stay silent.
    mock_notify.assert_awaited_once()
    mock_paused.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_enabled_pauses_print_and_flags_websocket_event():
    """With the setting on and a tray unassigned, the print is paused once."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}
    pause_calls = []
    client = SimpleNamespace(pause_print=lambda: (pause_calls.append(1), True)[1])

    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(
        _missing_a2_session(), "true", client
    )
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused as mock_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    assert pause_calls == [1]
    mock_ws.assert_awaited_once()
    assert mock_ws.await_args.kwargs["paused"] is True
    assert mock_ws.await_args.kwargs["missing_slots"] == [{"slot": "A2", "profile": "Unknown", "color": "Unknown"}]
    # The paused event fires *instead of* the warn-only one — never both, or the
    # user gets two pushes for one print.
    mock_paused.assert_awaited_once()
    assert mock_paused.await_args.kwargs["missing_slots"] == [{"slot": "A2", "profile": "Unknown", "color": "Unknown"}]
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_enabled_but_all_trays_assigned_does_not_pause():
    """The setting must not pause a print whose trays are all bound."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}
    pause_calls = []
    client = SimpleNamespace(pause_print=lambda: (pause_calls.append(1), True)[1])

    session = _FakeSession(
        "Printer A",
        legacy=[SimpleNamespace(ams_id=0, tray_id=0), SimpleNamespace(ams_id=0, tray_id=1)],
    )
    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(session, "true", client)
    with p_session, p_status, p_ws as mock_ws, p_notify, p_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    assert pause_calls == []
    mock_ws.assert_not_awaited()


@pytest.mark.asyncio
async def test_unassigned_but_unused_tray_is_ignored():
    """Only trays the job prints from matter.

    Print uses A2 alone, and A2 is assigned. A1 has no spool bound but the job
    never touches it, so it must not warn and must not pause — otherwise every
    print on a partly-loaded AMS would stall.
    """
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [1], "raw_data": {}}  # global tray 1 == A2 only
    pause_calls = []
    client = SimpleNamespace(pause_print=lambda: (pause_calls.append(1), True)[1])

    # Only A2 is bound; A1 (global tray 0) is deliberately left unassigned.
    session = _FakeSession("Printer A", legacy=[SimpleNamespace(ams_id=0, tray_id=1)])
    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(session, "true", client)
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused as mock_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    assert pause_calls == []
    mock_ws.assert_not_awaited()
    mock_notify.assert_not_awaited()
    mock_paused.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_resolvable_mapping_never_pauses():
    """Fail open: with no AMS mapping we can't tell which trays are used."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": None, "raw_data": {}}
    pause_calls = []
    client = SimpleNamespace(pause_print=lambda: (pause_calls.append(1), True)[1])

    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(
        _missing_a2_session(), "true", client
    )
    with p_session, p_status, p_ws as mock_ws, p_notify, p_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    assert pause_calls == []
    mock_ws.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_command_rejected_still_warns():
    """A refused pause must not swallow the warning, and must report paused=False."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}
    client = SimpleNamespace(pause_print=lambda: False)  # e.g. printer disconnected

    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(
        _missing_a2_session(), "true", client
    )
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused as mock_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_awaited_once()
    assert mock_ws.await_args.kwargs["paused"] is False
    # It never actually paused, so the user must get the warn-only push, not the
    # "print paused, go assign a spool" one.
    mock_notify.assert_awaited_once()
    mock_paused.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_mqtt_client_still_warns():
    """No client for the printer: warn, don't raise."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}

    p_session, p_status, p_ws, p_notify, p_paused, p_setting, p_client = _pause_patches(
        _missing_a2_session(), "true", None
    )
    with p_session, p_status, p_ws as mock_ws, p_notify as mock_notify, p_paused as mock_paused, p_setting, p_client:
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_awaited_once()
    assert mock_ws.await_args.kwargs["paused"] is False
    mock_notify.assert_awaited_once()
    mock_paused.assert_not_awaited()


@pytest.mark.asyncio
async def test_setting_lookup_failure_does_not_suppress_warning():
    """A broken settings read must still let the user hear about the missing spool."""
    logger = logging.getLogger(__name__)
    data = {"ams_mapping": [0, 1], "raw_data": {}}

    p_session, p_status, p_ws, p_notify, p_paused = _patches(_missing_a2_session())
    with (
        p_session,
        p_status,
        p_ws as mock_ws,
        p_notify as mock_notify,
        p_paused,
        patch(
            "backend.app.api.routes.settings.get_setting",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db gone"),
        ),
    ):
        await check_spool_assignments_on_print_start(1, data, logger)

    mock_ws.assert_awaited_once()
    assert mock_ws.await_args.kwargs["paused"] is False
    mock_notify.assert_awaited_once()
