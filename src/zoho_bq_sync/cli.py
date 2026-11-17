"""Command line entry point (also the Cloud Run Job container entrypoint).

  zoho-bq-sync sync       [--modules A,B] [--full | --since ISO [--until ISO]] [--dry-run]
  zoho-bq-sync discover   [--modules A,B] [--compare-bigquery]
  zoho-bq-sync reconcile  [--modules A,B]
  zoho-bq-sync freshness  [--max-age-hours 26]

Exit codes: 0 success, 1 sync/config failure, 2 stale data (freshness).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import Settings, load_settings
from .errors import SyncError
from .logging_setup import configure_logging, log_event
from .schema.builder import build_module_plan
from .schema.evolution import plan_schema_change
from .zoho.auth import ZohoOAuth
from .zoho.client import ZohoClient
from .zoho.metadata import MetadataClient, select_modules
from .zoho.records import RecordsClient

logger = logging.getLogger("zoho_bq_sync")


def _parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 datetime: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_zoho(settings: Settings) -> tuple[ZohoClient, MetadataClient, RecordsClient]:
    z = settings.zoho
    assert z is not None
    auth = ZohoOAuth(z.accounts_url, z.client_id, z.client_secret, z.refresh_token, timeout_seconds=z.timeout_seconds)
    client = ZohoClient(
        auth,
        z.api_domain,
        api_version=z.api_version,
        timeout_seconds=z.timeout_seconds,
        max_retries=z.max_retries,
        backoff_base_seconds=z.backoff_base_seconds,
        backoff_max_seconds=z.backoff_max_seconds,
        max_calls=z.max_api_calls_per_run,
    )
    return client, MetadataClient(client), RecordsClient(client, per_page=z.page_size)


def build_warehouse(settings: Settings) -> Any:
    from .warehouse.bigquery import BigQueryWarehouse  # keeps `discover` usable without GCP credentials

    assert settings.bigquery is not None
    return BigQueryWarehouse(settings.bigquery)


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


# -- commands -------------------------------------------------------------------------------
def cmd_sync(args: argparse.Namespace) -> int:
    from .sync.engine import SyncEngine, SyncRunFailed

    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    client, metadata, records = build_zoho(settings)
    engine = SyncEngine(
        metadata=metadata,
        records=records,
        warehouse=build_warehouse(settings),
        settings=settings.sync,
        api_calls=lambda: client.calls_made,
    )
    try:
        report = engine.run(modules=args.modules, full=args.full, since=args.since, until=args.until, dry_run=args.dry_run)
    except SyncRunFailed as exc:
        if args.summary:
            _print(exc.report.to_dict())
        return 1
    if args.summary:
        _print(report.to_dict())
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    settings = load_settings(require_bigquery=args.compare_bigquery)
    configure_logging(settings.log_level, settings.log_format)
    _, metadata, _ = build_zoho(settings)
    warehouse = build_warehouse(settings) if args.compare_bigquery else None
    include = args.modules or list(settings.sync.modules)
    modules = select_modules(metadata.list_modules(), include, settings.sync.exclude_modules)

    for module in modules:
        fields = metadata.get_fields(module.api_name)
        subforms = {f.api_name: metadata.get_fields(f.associated_module or f.api_name) for f in fields if f.is_subform}
        plan = build_module_plan(
            module.api_name,
            fields,
            subforms,
            table_name_overrides=settings.sync.table_name_overrides,
            exclude_fields=settings.sync.exclude_fields,
            store_raw_json=settings.sync.store_raw_json,
        )
        zoho_types = {f.api_name: f.data_type for f in fields}
        for sub in subforms.values():
            zoho_types.update({f.api_name: f.data_type for f in sub})
        strategy = "GET /{module}/{id} per changed record" if plan.requires_record_fetch else "list API"
        print(f"\n=== {module.api_name} ({module.generated_type}) -> {plan.parent.name}   extraction: {strategy}")
        for spec in (plan.parent, *plan.children):
            status: dict[str, str] = {}
            if warehouse is not None:
                change = plan_schema_change(spec, warehouse.get_table_columns(spec.name))
                status = {c.name: "NEW" for c in change.added} if not change.create else {c.name: "CREATE" for c in spec.columns}
            header = spec.name if not spec.parent_key else f"{spec.name}  (subform {spec.subform_field}, FK parent_id)"
            print(f"\n  {header}")
            print(f"  {'column':<36} {'bigquery':<18} {'zoho field':<28} {'zoho type':<20} {'':<6}")
            for col in spec.columns:
                bq_type = col.type + (" REPEATED" if col.mode == "REPEATED" else "")
                print(
                    f"  {col.name:<36} {bq_type:<18} {col.source or '(pipeline)':<28} "
                    f"{zoho_types.get(col.source or '', ''):<20} {status.get(col.name, ''):<6}"
                )
        for name, reason in plan.skipped_fields:
            print(f"  skipped: {name} - {reason}")
        for warning in plan.warnings:
            print(f"  warning: {warning}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)
    _, metadata, records = build_zoho(settings)
    warehouse = build_warehouse(settings)
    include = args.modules or list(settings.sync.modules)
    rows = []
    for module in select_modules(metadata.list_modules(), include, settings.sync.exclude_modules):
        state = warehouse.load_state(module.api_name)
        if state is None:
            rows.append({"module": module.api_name, "status": "NOT_SYNCED"})
            continue
        zoho = records.count(module.api_name)
        bq = warehouse.count_active(state.table_name)
        rows.append({"module": module.api_name, "table": state.table_name, "zoho": zoho, "bigquery_active": bq, "status": "OK" if zoho == bq else "MISMATCH"})
    _print(rows)
    return 0 if all(r["status"] == "OK" for r in rows) else 1


def cmd_freshness(args: argparse.Namespace) -> int:
    settings = load_settings(require_zoho=False)
    configure_logging(settings.log_level, settings.log_format)
    warehouse = build_warehouse(settings)
    now = datetime.now(timezone.utc)
    states = warehouse.read_states()
    report = []
    stale = not states
    for state in states:
        age = (now - state.updated_at).total_seconds() / 3600 if state.updated_at else None
        is_stale = age is None or age > args.max_age_hours
        stale = stale or is_stale
        report.append({"module": state.module, "last_success": state.updated_at, "age_hours": round(age, 2) if age is not None else None, "stale": is_stale})
    log_event(logger, "freshness_check", logging.ERROR if stale else logging.INFO, max_age_hours=args.max_age_hours, modules=report)
    _print(report)
    return 2 if stale else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zoho-bq-sync", description="Zoho CRM v8 -> BigQuery replication")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="run a synchronization")
    sync.add_argument("--modules", type=_csv, help="comma-separated module api names (default: ZOHO_MODULES or all)")
    sync.add_argument("--full", action="store_true", help="manual full resynchronization with deletion reconciliation")
    sync.add_argument("--since", type=_parse_ts, help="replay window start (ISO 8601); checkpoint is not moved")
    sync.add_argument("--until", type=_parse_ts, help="replay window end (ISO 8601)")
    sync.add_argument("--dry-run", action="store_true", help="extract and plan, write nothing to BigQuery")
    sync.add_argument("--summary", action="store_true", help="print a JSON run summary to stdout at the end")
    sync.set_defaults(func=cmd_sync)

    discover = sub.add_parser("discover", help="show modules, fields and the generated BigQuery schema")
    discover.add_argument("--modules", type=_csv)
    discover.add_argument("--compare-bigquery", action="store_true", help="mark columns that do not exist in BigQuery yet")
    discover.set_defaults(func=cmd_discover)

    reconcile = sub.add_parser("reconcile", help="compare Zoho record counts with active BigQuery rows (read-only)")
    reconcile.add_argument("--modules", type=_csv)
    reconcile.set_defaults(func=cmd_reconcile)

    freshness = sub.add_parser("freshness", help="exit 2 if any module has not synced successfully recently")
    freshness.add_argument("--max-age-hours", type=float, default=26.0)
    freshness.set_defaults(func=cmd_freshness)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SyncError as exc:
        if not logging.getLogger().handlers:
            configure_logging()
        log_event(logger, "command_failed", logging.ERROR, command=args.command, error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
