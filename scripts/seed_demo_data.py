#!/usr/bin/env python3
"""Create and mutate SYNTHETIC Wedding_Bookings records in a Zoho developer org.

Every record name starts with WB-SYN-. Uses the same env configuration as the pipeline
(ZOHO_* variables); the OAuth token needs ZohoCRM.modules.ALL for writes.

  python scripts/seed_demo_data.py check
  python scripts/seed_demo_data.py create --count 8 [--seed 42] [--dry-run]
  python scripts/seed_demo_data.py list
  python scripts/seed_demo_data.py update --id <booking_id> --field Booking_Status --value Confirmed
  python scripts/seed_demo_data.py add-service --id <booking_id>
  python scripts/seed_demo_data.py remove-service --id <booking_id> [--row-id <subform_row_id>]
  python scripts/seed_demo_data.py delete --id <booking_id>
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zoho_bq_sync.cli import build_zoho  # noqa: E402
from zoho_bq_sync.config import load_settings  # noqa: E402
from zoho_bq_sync.demo.synthetic import (  # noqa: E402
    MODULE,
    NAME_PREFIX,
    PARENT_FIELDS,
    SUBFORM,
    SUBFORM_FIELDS,
    generate_bookings,
    generate_service,
)
from zoho_bq_sync.logging_setup import configure_logging  # noqa: E402


def _check_results(body: dict[str, Any] | None, action: str) -> list[str]:
    """Zoho write APIs answer per record; one failed record must fail the command."""
    results = (body or {}).get("data") or []
    ids, errors = [], []
    for item in results:
        if item.get("status") == "success":
            ids.append(str((item.get("details") or {}).get("id")))
        else:
            errors.append(f"{item.get('code')}: {item.get('message')} {json.dumps(item.get('details'))}")
    if errors or not results:
        raise SystemExit(f"{action} failed: {errors or 'empty response'}")
    return ids


def cmd_check(metadata: Any, _records: Any, _client: Any, _args: argparse.Namespace) -> int:
    fields = {f.api_name: f for f in metadata.get_fields(MODULE)}
    missing = [f for f in PARENT_FIELDS if f not in fields]
    subform = fields.get(SUBFORM)
    if subform is None or not subform.is_subform:
        missing.append(f"{SUBFORM} (subform)")
        sub_missing: list[str] = list(SUBFORM_FIELDS)
    else:
        sub_fields = {f.api_name for f in metadata.get_fields(subform.associated_module or SUBFORM)}
        sub_missing = [f for f in SUBFORM_FIELDS if f not in sub_fields]
    if missing or sub_missing:
        print(f"Missing in {MODULE}: {missing}\nMissing in {SUBFORM}: {sub_missing}\nSee docs/zoho_setup.md")
        return 1
    print(f"OK: {MODULE} has all {len(PARENT_FIELDS)} fields and subform {SUBFORM} (module {subform.associated_module})")
    return 0


def cmd_create(_metadata: Any, _records: Any, client: Any, args: argparse.Namespace) -> int:
    bookings = generate_bookings(args.count, seed=args.seed, start_number=args.start)
    if args.dry_run:
        print(json.dumps({"data": bookings}, indent=2, ensure_ascii=False))
        print(f"[dry-run] {len(bookings)} SYNTHETIC bookings generated, nothing sent to Zoho", file=sys.stderr)
        return 0
    created: list[str] = []
    for start in range(0, len(bookings), 100):  # insert API: max 100 records per call
        response = client.post(MODULE, json={"data": bookings[start : start + 100], "trigger": []})
        created.extend(_check_results(response.body, "insert"))
    for booking, record_id in zip(bookings, created):
        print(f"{record_id}  {booking['Name']}  services={len(booking[SUBFORM])}")
    return 0


def cmd_list(_metadata: Any, records: Any, _client: Any, _args: argparse.Namespace) -> int:
    for page in records.iter_pages(MODULE, ["Name", "Booking_Status", "Modified_Time"], sort_by="Modified_Time", sort_order="asc"):
        for r in page.records:
            if str(r.get("Name", "")).startswith(NAME_PREFIX):
                print(f"{r['id']}  {r['Modified_Time']}  {r.get('Booking_Status')}  {r['Name']}")
    return 0


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def cmd_update(_metadata: Any, _records: Any, client: Any, args: argparse.Namespace) -> int:
    response = client.put(MODULE, json={"data": [{"id": args.id, args.field: _parse_value(args.value)}], "trigger": []})
    _check_results(response.body, "update")
    print(f"Updated {args.id}: {args.field} = {_parse_value(args.value)!r}")
    return 0


def _rows(records: Any, booking_id: str) -> list[dict[str, Any]]:
    record = records.get_record(MODULE, booking_id)
    if record is None:
        raise SystemExit(f"Booking {booking_id} not found")
    return record.get(SUBFORM) or []


def cmd_add_service(_metadata: Any, records: Any, client: Any, args: argparse.Namespace) -> int:
    rows = _rows(records, args.id)
    rng = random.Random(args.seed)
    new_row = generate_service(rng, date.fromisoformat("2027-06-15"), 10)
    # Existing rows are listed by id so they are kept whatever the update semantics.
    payload = [{"id": r["id"]} for r in rows] + [new_row]
    _check_results(client.put(MODULE, json={"data": [{"id": args.id, SUBFORM: payload}], "trigger": []}).body, "add-service")
    print(f"Added service '{new_row['Service_Name']}' to {args.id} ({len(rows)} -> {len(rows) + 1} rows)")
    return 0


def cmd_remove_service(_metadata: Any, records: Any, client: Any, args: argparse.Namespace) -> int:
    rows = _rows(records, args.id)
    if not rows:
        raise SystemExit("Booking has no subform rows")
    target = args.row_id or rows[-1]["id"]
    if target not in {r["id"] for r in rows}:
        raise SystemExit(f"Row {target} is not part of booking {args.id}")
    payload = [{"id": r["id"]} if r["id"] != target else {"id": target, "_delete": None} for r in rows]
    _check_results(client.put(MODULE, json={"data": [{"id": args.id, SUBFORM: payload}], "trigger": []}).body, "remove-service")
    print(f"Removed subform row {target} from {args.id} ({len(rows)} -> {len(rows) - 1} rows)")
    return 0


def cmd_delete(_metadata: Any, _records: Any, client: Any, args: argparse.Namespace) -> int:
    _check_results(client.delete(MODULE, params={"ids": args.id}).body, "delete")
    print(f"Deleted {args.id} (moved to the Zoho recycle bin)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    create = sub.add_parser("create")
    create.add_argument("--count", type=int, default=8)
    create.add_argument("--seed", type=int, default=42)
    create.add_argument("--start", type=int, default=1, help="first WB-SYN number")
    create.add_argument("--dry-run", action="store_true", help="print generated JSON, do not call Zoho")
    create.set_defaults(func=cmd_create)
    sub.add_parser("list").set_defaults(func=cmd_list)
    update = sub.add_parser("update")
    update.add_argument("--id", required=True)
    update.add_argument("--field", required=True)
    update.add_argument("--value", required=True, help="JSON literal or plain string")
    update.set_defaults(func=cmd_update)
    add = sub.add_parser("add-service")
    add.add_argument("--id", required=True)
    add.add_argument("--seed", type=int, default=7)
    add.set_defaults(func=cmd_add_service)
    remove = sub.add_parser("remove-service")
    remove.add_argument("--id", required=True)
    remove.add_argument("--row-id")
    remove.set_defaults(func=cmd_remove_service)
    delete = sub.add_parser("delete")
    delete.add_argument("--id", required=True)
    delete.set_defaults(func=cmd_delete)
    args = parser.parse_args()

    if args.command == "create" and args.dry_run:
        return cmd_create(None, None, None, args)
    settings = load_settings(require_bigquery=False)
    configure_logging(settings.log_level, "text")
    client, metadata, records = build_zoho(settings)
    return int(args.func(metadata, records, client, args))


if __name__ == "__main__":
    raise SystemExit(main())
