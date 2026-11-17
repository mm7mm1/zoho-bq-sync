"""End-to-end orchestration on MOCK Zoho + in-memory warehouse (no network)."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes import FakeClock, FakeMetadata, FakeRecords, InMemoryWarehouse, zf
from zoho_bq_sync.config import SyncSettings
from zoho_bq_sync.errors import SafetyCheckError
from zoho_bq_sync.sync.engine import SyncEngine, SyncRunFailed
from zoho_bq_sync.zoho.metadata import ZohoModule

UTC = timezone.utc
MODULE = "Wedding_Bookings"
SETTINGS = SyncSettings(modules=(MODULE,), overlap_seconds=300, table_name_overrides={"Booked_Services": "wedding_bookings_services"})


def parent_fields():
    return [
        zf("Name", "text", "10"),
        zf("Modified_Time", "datetime", "11"),
        zf("Package_Price", "currency", "12"),
        zf("Booking_Status", "picklist", "13"),
        zf("Booked_Services", "subform", "14", associated_module="Booked_Services"),
    ]


def service_fields():
    return [zf("Service_Name", "text", "20"), zf("Unit_Price", "currency", "21"), zf("Parent_Id", "lookup", "22")]


def booking(rid, name, services, status="Inquiry"):
    return {
        "id": rid,
        "Name": name,
        "Package_Price": sum(s["Unit_Price"] for s in services),
        "Booking_Status": status,
        "Booked_Services": [{"Parent_Id": {"id": rid}, **s} for s in services],
    }


@pytest.fixture
def env():
    clock = FakeClock(datetime(2026, 11, 17, 9, 0, tzinfo=UTC))
    metadata = FakeMetadata(
        [ZohoModule("Wedding_Bookings", generated_type="custom"), ZohoModule("Booked_Services", generated_type="subform")],
        {MODULE: parent_fields(), "Booked_Services": service_fields()},
    )
    records = FakeRecords(clock, subform_fields=["Booked_Services"])
    records.upsert(MODULE, booking("1", "WB-SYN-0001", [{"id": "s1", "Service_Name": "Civil registration", "Unit_Price": 650}, {"id": "s2", "Service_Name": "Photographer", "Unit_Price": 1200}]))
    clock.advance(60)
    records.upsert(MODULE, booking("2", "WB-SYN-0002", [{"id": "s3", "Service_Name": "Venue", "Unit_Price": 2100}]))
    clock.advance(60)
    warehouse = InMemoryWarehouse()
    return clock, metadata, records, warehouse


def run(env, settings=SETTINGS, **kw):
    clock, metadata, records, warehouse = env
    clock.advance(600)
    engine = SyncEngine(metadata=metadata, records=records, warehouse=warehouse, settings=settings, clock=clock)
    return engine.run(**kw)


def test_initial_full_load_creates_parent_and_child_tables(env):
    _, _, records, wh = env
    report = run(env)
    (result,) = report.modules
    assert result.mode == "full" and result.status == "success"
    assert set(wh.tables) == {"wedding_bookings", "wedding_bookings_services"}
    assert set(wh.active("wedding_bookings")) == {"1", "2"}
    children = wh.active("wedding_bookings_services")
    assert {k: v["parent_id"] for k, v in children.items()} == {"s1": "1", "s2": "1", "s3": "2"}
    assert children["s2"]["unit_price"] == "1200"
    # Subform module: list call asks only for ids + Modified_Time, then one GET per record.
    assert records.list_calls[0]["fields"] == ["Modified_Time"] and records.detail_calls == 2
    state = wh.states[MODULE]
    assert state.last_modified_time == datetime(2026, 11, 17, 9, 1, tzinfo=UTC)
    assert all(r["status"] == "OK" for r in result.reconciliation)
    assert wh.runs[0]["status"] == "success" and wh.runs[0]["mode"] == "full"


def test_incremental_uses_checkpoint_minus_overlap_and_upserts(env):
    clock, _, records, wh = env
    run(env)
    clock.advance(60)
    changed = records.store[MODULE]["2"]
    changed["Booking_Status"] = "Confirmed"
    records.upsert(MODULE, changed)

    report = run(env)
    (result,) = report.modules
    assert result.mode == "incremental"
    assert records.list_calls[-1]["since"] == datetime(2026, 11, 17, 9, 1, tzinfo=UTC) - timedelta(seconds=300)
    assert wh.rows("wedding_bookings")["2"]["booking_status"] == "Confirmed"
    assert records.deleted_calls, "deleted-records API must be polled in incremental mode"
    assert wh.states[MODULE].last_modified_time > datetime(2026, 11, 17, 9, 1, tzinfo=UTC)


def test_new_custom_field_is_added_and_loaded_without_code_change(env):
    clock, metadata, records, wh = env
    run(env)
    # Demo step 5: an admin adds "Apostille Deadline" (Date) in Zoho and fills it on one booking.
    metadata.fields[MODULE].append(zf("Apostille_Deadline", "date", "15", custom_field=True))
    clock.advance(60)
    rec = records.store[MODULE]["1"]
    rec["Apostille_Deadline"] = "2027-05-20"
    records.upsert(MODULE, rec)

    report = run(env)
    (result,) = report.modules
    assert result.new_columns == ["wedding_bookings.apostille_deadline"]
    assert ("add", "wedding_bookings", ["apostille_deadline"]) in wh.ddl
    assert wh.rows("wedding_bookings")["1"]["apostille_deadline"] == "2027-05-20"
    assert wh.rows("wedding_bookings")["2"].get("apostille_deadline") is None  # history kept, untouched row stays NULL


def test_backfill_flag_turns_new_field_into_full_resync(env):
    _, metadata, _, _ = env
    run(env)
    metadata.fields[MODULE].append(zf("Apostille_Deadline", "date", "15"))
    report = run(env, settings=SyncSettings(**{**SETTINGS.__dict__, "backfill_on_new_fields": True}))
    assert report.modules[0].mode == "full" and "backfill" in report.modules[0].mode_reason


def test_removed_subform_row_is_soft_deleted(env):
    clock, _, records, wh = env
    run(env)
    clock.advance(60)
    rec = records.store[MODULE]["1"]
    rec["Booked_Services"] = [r for r in rec["Booked_Services"] if r["id"] != "s2"]
    records.upsert(MODULE, rec)

    report = run(env)
    services = wh.rows("wedding_bookings_services")
    assert services["s2"]["_is_deleted"] is True and services["s1"]["_is_deleted"] is False
    assert services["s3"]["_is_deleted"] is False  # other parents are not touched
    check = next(r for r in report.modules[0].reconciliation if r["check_name"] == "child_rows_for_synced_parents")
    # The 300 s overlap window re-reads booking 2 as well: both parents are reconciled.
    assert check["status"] == "OK" and check["expected"] == 2


def test_deleted_parent_cascades_to_children(env):
    clock, _, records, wh = env
    run(env)
    clock.advance(60)
    records.delete(MODULE, "2")

    report = run(env)
    assert report.modules[0].deleted_records == 1
    assert wh.rows("wedding_bookings")["2"]["_is_deleted"] is True
    assert wh.rows("wedding_bookings_services")["s3"]["_is_deleted"] is True
    assert wh.count_active("wedding_bookings") == 1


def test_full_resync_reconciles_missing_rows(env):
    _, _, records, wh = env
    run(env)
    records.store[MODULE].pop("2")  # vanished without a deleted-records entry (e.g. older than retention)
    report = run(env, full=True)
    assert report.modules[0].mode == "full"
    assert wh.rows("wedding_bookings")["2"]["_is_deleted"] is True
    assert wh.rows("wedding_bookings_services")["s3"]["_is_deleted"] is True


def test_full_resync_refuses_mass_delete(env):
    _, _, records, wh = env
    run(env)
    records.store[MODULE].clear()
    with pytest.raises(SyncRunFailed) as exc:
        run(env, full=True)
    assert "SafetyCheckError" in exc.value.report.modules[0].error
    assert wh.count_active("wedding_bookings") == 2


def test_failed_apply_keeps_checkpoint_and_tables(env):
    clock, _, records, wh = env
    run(env)
    checkpoint = wh.states[MODULE].last_modified_time
    clock.advance(60)
    rec = records.store[MODULE]["1"]
    rec["Booking_Status"] = "Completed"
    records.upsert(MODULE, rec)
    wh.fail_next_apply = RuntimeError("BigQuery transaction aborted")

    with pytest.raises(SyncRunFailed) as exc:
        run(env)
    assert exc.value.report.modules[0].status == "failed"
    assert wh.states[MODULE].last_modified_time == checkpoint
    assert wh.rows("wedding_bookings")["1"]["booking_status"] == "Inquiry"
    assert wh.runs[-1]["status"] == "failed"

    # Safe retry: the next run re-reads the same window and converges.
    run(env)
    assert wh.rows("wedding_bookings")["1"]["booking_status"] == "Completed"


def test_replaying_the_same_window_is_idempotent(env):
    _, _, _, wh = env
    run(env)
    snapshot = {k: dict(v) for k, v in wh.rows("wedding_bookings").items()}
    run(env, since=datetime(2026, 11, 17, 8, 0, tzinfo=UTC))
    after = wh.rows("wedding_bookings")
    assert {k: {c: v for c, v in r.items() if not c.startswith("_")} for k, r in after.items()} == {
        k: {c: v for c, v in r.items() if not c.startswith("_")} for k, r in snapshot.items()
    }
    assert wh.count_active("wedding_bookings_services") == 3


def test_window_replay_does_not_move_checkpoint(env):
    _, _, _, wh = env
    run(env)
    checkpoint = wh.states[MODULE].last_modified_time
    report = run(env, since=datetime(2026, 11, 1, tzinfo=UTC), until=datetime(2026, 11, 17, 9, 0, 30, tzinfo=UTC))
    assert report.modules[0].mode == "window"
    assert wh.states[MODULE].last_modified_time == checkpoint


def test_dry_run_writes_nothing(env):
    _, _, _, wh = env
    report = run(env, dry_run=True)
    assert report.modules[0].status == "dry_run"
    assert wh.tables == {} and wh.states == {} and wh.runs == [] and wh.writes == 0
