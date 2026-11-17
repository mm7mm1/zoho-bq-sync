"""Value coercion into JSON-serializable values that BigQuery load jobs accept.

NUMERIC is emitted as a decimal string (no float rounding), TIMESTAMP as UTC ISO 8601,
DATE as YYYY-MM-DD. Values that do not fit raise CoercionError: a silent NULL would
hide a type-mapping bug, so the module fails loudly instead.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from ..errors import CoercionError
from ..schema.model import BIGNUMERIC, BOOL, DATE, FLOAT64, INT64, NUMERIC, REPEATED, STRING, TIMESTAMP, Column

_NUMERIC_SCALE = Decimal("1e-9")
_NUMERIC_MAX_INTEGER_DIGITS = 29  # NUMERIC(38, 9)


def to_json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def coerce_value(value: Any, column: Column) -> Any:
    if column.mode == REPEATED:
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        return [_scalar(item, column.type) for item in items if item is not None]
    if value is None:
        return None
    return _scalar(value, column.type)


def _scalar(value: Any, bq_type: str) -> Any:
    if bq_type == STRING:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list, tuple)):
            return to_json_text(value)
        return str(value)

    if isinstance(value, str) and value.strip() == "":
        return None

    if bq_type == INT64:
        if isinstance(value, bool):
            raise CoercionError(f"boolean {value!r} is not an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
            try:
                as_decimal = Decimal(value.strip())
            except InvalidOperation:
                raise CoercionError(f"{value!r} is not an integer") from None
            if as_decimal == as_decimal.to_integral_value():
                return int(as_decimal)
        raise CoercionError(f"{value!r} is not an integer")

    if bq_type == FLOAT64:
        if isinstance(value, bool):
            raise CoercionError(f"boolean {value!r} is not a number")
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise CoercionError(f"{value!r} is not a number") from None
        if math.isnan(result) or math.isinf(result):
            raise CoercionError(f"{value!r} cannot be loaded into FLOAT64 via JSON")
        return result

    if bq_type in (NUMERIC, BIGNUMERIC):
        if isinstance(value, bool):
            raise CoercionError(f"boolean {value!r} is not a decimal")
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            raise CoercionError(f"{value!r} is not a decimal") from None
        if not number.is_finite():
            raise CoercionError(f"{value!r} is not a finite decimal")
        if bq_type == NUMERIC:
            if number.as_tuple().exponent < -9:  # type: ignore[operator]
                number = number.quantize(_NUMERIC_SCALE, rounding=ROUND_HALF_EVEN)
            integer_digits = len(str(abs(int(number)))) if int(number) else 0
            if integer_digits > _NUMERIC_MAX_INTEGER_DIGITS:
                raise CoercionError(f"{value!r} exceeds NUMERIC precision; widen the column to BIGNUMERIC")
        return format(number, "f")

    if bq_type == BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise CoercionError(f"{value!r} is not a boolean")

    if bq_type == DATE:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError:
            raise CoercionError(f"{value!r} is not a YYYY-MM-DD date") from None

    if bq_type == TIMESTAMP:
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).strip())
            except ValueError:
                raise CoercionError(f"{value!r} is not an ISO 8601 datetime") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    raise CoercionError(f"Unsupported BigQuery type {bq_type}")
