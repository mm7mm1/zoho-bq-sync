"""Row-count reconciliation between Zoho and BigQuery.

Checks:
- zoho_count_vs_bq_active: GET /{module}/actions/count vs active (non-deleted) BigQuery rows.
- child_rows_for_synced_parents: for every parent synced in this run, the number of subform
  rows Zoho returned vs active child rows in BigQuery (Zoho has no subform count API).
- child_rows_total (full resync only): all extracted subform rows vs active child rows.
A mismatch is logged and recorded; it fails the run only with FAIL_ON_RECONCILIATION_MISMATCH,
because records edited during the run legitimately make counts differ for a moment.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from ..logging_setup import log_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    table_name: str
    check_name: str
    expected: int
    actual: int
    details: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        return "OK" if self.expected == self.actual else "MISMATCH"

    def to_row(self, run_id: str, module: str, checked_at: datetime) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "module": module,
            "table_name": self.table_name,
            "check_name": self.check_name,
            "expected": self.expected,
            "actual": self.actual,
            "difference": self.actual - self.expected,
            "status": self.status,
            "details": json.dumps(self.details, default=str) if self.details else None,
            "checked_at": checked_at.isoformat(),
        }


def child_count_check(table: str, expected_by_parent: Mapping[str, int], actual_by_parent: Mapping[str, int]) -> Check:
    mismatches = {
        pid: {"zoho": exp, "bigquery": actual_by_parent.get(pid, 0)}
        for pid, exp in expected_by_parent.items()
        if actual_by_parent.get(pid, 0) != exp
    }
    return Check(
        table_name=table,
        check_name="child_rows_for_synced_parents",
        expected=sum(expected_by_parent.values()),
        actual=sum(actual_by_parent.get(pid, 0) for pid in expected_by_parent),
        details={"parents_checked": len(expected_by_parent), "mismatched_parents": dict(list(mismatches.items())[:20])}
        if mismatches
        else {"parents_checked": len(expected_by_parent)},
    )


def log_checks(checks: list[Check]) -> None:
    for check in checks:
        log_event(
            logger,
            "reconciliation" if check.status == "OK" else "reconciliation_mismatch",
            logging.INFO if check.status == "OK" else logging.WARNING,
            table=check.table_name,
            check=check.check_name,
            expected=check.expected,
            actual=check.actual,
            status=check.status,
            details=check.details,
        )
