import logging

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.models.printer import Printer
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import printer_manager


def _global_tray_from_assignment(ams_id: int, tray_id: int) -> int:
    """Convert an assignment tuple to Bambuddy global tray ID."""
    if ams_id in (254, 255):
        return 254 + tray_id
    if ams_id >= 128:
        return ams_id
    return ams_id * 4 + tray_id


def _slot_label_from_global_tray(global_tray_id: int) -> str:
    """Return a human-readable slot label from a global tray ID."""
    if global_tray_id == 254:
        return "Ext-L"
    if global_tray_id == 255:
        return "Ext-R"
    if global_tray_id >= 128:
        return f"HT-{chr(65 + (global_tray_id - 128))}"
    # 24-27 = A2L AMS-Lite (normalised unit 6); see a2l-am-unit-16.
    if 24 <= global_tray_id <= 27:
        return f"Lite-{(global_tray_id % 4) + 1}"
    ams_id = global_tray_id // 4
    tray_id = global_tray_id % 4
    return f"{chr(65 + ams_id)}{tray_id + 1}"


def _tray_profile_and_color_for_global_id(state: PrinterState | None, global_tray_id: int) -> tuple[str, str]:
    """Resolve expected tray material/profile and color for a global tray ID from current printer state."""
    if not state or not state.raw_data:
        return ("Unknown", "Unknown")

    ams_raw = state.raw_data.get("ams", {})
    ams_units = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []

    vt_trays = state.raw_data.get("vt_tray", [])
    if not isinstance(vt_trays, list):
        vt_trays = []

    for tray in vt_trays:
        if not isinstance(tray, dict):
            continue
        if int(tray.get("id", -1)) == global_tray_id:
            profile = tray.get("tray_sub_brands") or tray.get("tray_type") or "Unknown"
            color = tray.get("tray_color") or "Unknown"
            return (profile, color)

    for ams in ams_units:
        if not isinstance(ams, dict):
            continue
        ams_id = int(ams.get("id", -1))
        trays = ams.get("tray", [])
        if not isinstance(trays, list):
            continue
        for tray in trays:
            if not isinstance(tray, dict):
                continue
            tray_id = int(tray.get("id", -1))
            candidate = ams_id if ams_id >= 128 else (ams_id * 4 + tray_id)
            if candidate == global_tray_id:
                profile = tray.get("tray_sub_brands") or tray.get("tray_type") or "Unknown"
                color = tray.get("tray_color") or "Unknown"
                return (profile, color)

    return ("Unknown", "Unknown")


def _decode_mqtt_mapping_to_global_trays(mapping_raw: object) -> list[int]:
    """Decode printer MQTT mapping values into Bambuddy global tray IDs."""
    if not isinstance(mapping_raw, list) or not mapping_raw:
        return []

    decoded: list[int] = []
    for value in mapping_raw:
        try:
            if isinstance(value, int):
                encoded = value
            elif isinstance(value, str):
                encoded = int(value, 10)
            else:
                continue
        except ValueError:
            continue

        if encoded >= 65535:
            continue

        ams_hw_id = (encoded >> 8) & 0xFF
        slot = encoded & 0xFF

        if 0 <= ams_hw_id <= 3:
            decoded.append(ams_hw_id * 4 + (slot & 0x03))
        elif 128 <= ams_hw_id <= 135:
            decoded.append(ams_hw_id)
        elif ams_hw_id in (254, 255):
            decoded.append(255 if slot == 255 else 254)

    return decoded


async def check_spool_assignments_on_print_start(
    printer_id: int,
    data: dict,
    logger: logging.Logger,
) -> None:
    """Notify — and optionally pause the print — when print-start mapping references unassigned trays.

    An unassigned tray means the print's filament use is never debited from any
    spool: every deduction site in usage_tracker skips trays with no assignment,
    so inventory silently drifts. When pause_print_on_unassigned_spool is on we
    pause instead, giving the user a chance to assign a spool and resume.

    Pausing is safe for usage tracking: deduction happens only at print
    completion (from the 3MF slicer estimate, not incrementally), and
    _resolve_spool_id_for_tray falls back to the live SpoolAssignment row when
    the print-start snapshot has no entry for a tray. A spool assigned during
    the pause is therefore charged in full.

    This fires at most once per print: on_print_start is suppressed on
    PAUSE->RUNNING by the _was_running guard in bambu_mqtt, so resuming without
    assigning a spool is treated as a deliberate override and is not re-paused.
    """
    explicit_mapping = data.get("ams_mapping")
    explicit_values = (
        [value for value in explicit_mapping if isinstance(value, int)] if isinstance(explicit_mapping, list) else []
    )
    raw_mapping = data.get("raw_data", {}).get("mapping") if isinstance(data.get("raw_data"), dict) else None
    decoded_values = _decode_mqtt_mapping_to_global_trays(raw_mapping)
    mapping_values = explicit_values if explicit_values else decoded_values

    used_global_trays = {value for value in mapping_values if value >= 0}
    if not used_global_trays:
        return

    try:
        async with async_session() as db:
            printer = await db.get(Printer, printer_id)
            printer_name = printer.name if printer else f"Printer {printer_id}"

            # A tray is "assigned" if it has a row in EITHER table: the legacy
            # spool_assignment table (internal-inventory mode) or
            # spoolman_slot_assignments (Spoolman mode — the binding
            # source-of-truth since #1119). Querying only the legacy table
            # flagged every used tray as missing on every Spoolman-mode print
            # (#1473). Both tables expose printer_id / ams_id / tray_id in the
            # same shape, so _global_tray_from_assignment works on either.
            legacy_rows = (
                await db.execute(SpoolAssignment.__table__.select().where(SpoolAssignment.printer_id == printer_id))
            ).fetchall()
            spoolman_rows = (
                await db.execute(
                    SpoolmanSlotAssignment.__table__.select().where(SpoolmanSlotAssignment.printer_id == printer_id)
                )
            ).fetchall()
            assigned_global_trays = {
                _global_tray_from_assignment(row.ams_id, row.tray_id) for row in (*legacy_rows, *spoolman_rows)
            }

            missing_global = sorted(used_global_trays - assigned_global_trays)
            if not missing_global:
                return

            state = printer_manager.get_status(printer_id)
            missing_slots = []
            for global_id in missing_global:
                profile, color = _tray_profile_and_color_for_global_id(state, global_id)
                missing_slots.append(
                    {
                        "slot": _slot_label_from_global_tray(global_id),
                        "profile": profile,
                        "color": color,
                    }
                )

            paused = await _pause_for_missing_assignments(printer_id, missing_global, db, logger)

            await ws_manager.send_missing_spool_assignment(
                printer_id=printer_id,
                printer_name=printer_name,
                missing_slots=missing_slots,
                paused=paused,
            )

            # Exactly one of the two events fires: the paused one is actionable
            # (printer stopped, waiting on the user) and has its own provider
            # toggle, so sending both would double-notify.
            notify = (
                notification_service.on_print_paused_unassigned_spool
                if paused
                else notification_service.on_print_missing_spool_assignment
            )
            await notify(
                printer_id=printer_id,
                printer_name=printer_name,
                missing_slots=missing_slots,
                db=db,
            )
    except Exception as e:
        logger.warning("Missing spool-assignment notification failed: %s", e)


async def _pause_for_missing_assignments(
    printer_id: int,
    missing_global: list[int],
    db: AsyncSession,
    logger: logging.Logger,
) -> bool:
    """Pause the print if the user opted in. Returns True only if the pause was sent.

    Swallows its own errors so a failure here can never suppress the warning
    notification the caller sends next — the user must still be told.
    """
    from backend.app.api.routes.settings import get_setting

    try:
        pause_enabled = await get_setting(db, "pause_print_on_unassigned_spool")
        if not pause_enabled or pause_enabled.lower() != "true":
            return False

        client = printer_manager.get_client(printer_id)
        if not client:
            logger.warning(
                "Cannot pause printer %d for unassigned spools %s: no MQTT client",
                printer_id,
                missing_global,
            )
            return False

        if not client.pause_print():
            logger.warning("Pause command rejected for printer %d (unassigned spools %s)", printer_id, missing_global)
            return False

        logger.info("Paused printer %d: trays %s have no assigned spool", printer_id, missing_global)
        return True
    except Exception as e:
        logger.warning("Pause-on-unassigned-spool check failed for printer %d: %s", printer_id, e)
        return False
