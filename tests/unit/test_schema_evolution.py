import pytest

from zoho_bq_sync.errors import SchemaConflictError
from zoho_bq_sync.schema.evolution import ExistingColumn, canonical_type, plan_schema_change
from zoho_bq_sync.schema.model import Column, TableSpec


def spec(*columns):
    return TableSpec(name="wedding_bookings", zoho_module="Wedding_Bookings", columns=(Column("id", "STRING", "REQUIRED"), *columns))


def existing(*cols):
    return [ExistingColumn("id", "STRING", "REQUIRED"), *cols]


def test_missing_table_is_created():
    change = plan_schema_change(spec(Column("name", "STRING")), None)
    assert change.create and [c.name for c in change.added] == ["id", "name"]


def test_new_zoho_field_becomes_nullable_added_column():
    desired = spec(Column("name", "STRING"), Column("apostille_deadline", "DATE", source="Apostille_Deadline"))
    change = plan_schema_change(desired, existing(ExistingColumn("name", "STRING")))
    assert not change.create
    assert [(c.name, c.type, c.mode) for c in change.added] == [("apostille_deadline", "DATE", "NULLABLE")]


def test_required_column_is_added_as_nullable():
    change = plan_schema_change(spec(Column("parent_id", "STRING", "REQUIRED")), existing())
    assert change.added[0].mode == "NULLABLE"


def test_removed_zoho_field_keeps_column():
    change = plan_schema_change(spec(), existing(ExistingColumn("legacy_field", "STRING")))
    assert change.orphaned == ["legacy_field"] and not change.has_ddl


def test_legacy_type_names_are_equivalent():
    assert canonical_type("INTEGER") == "INT64" and canonical_type("BOOLEAN") == "BOOL" and canonical_type("FLOAT") == "FLOAT64"
    change = plan_schema_change(spec(Column("guests", "INT64"), Column("ok", "BOOL")), existing(ExistingColumn("guests", "INTEGER"), ExistingColumn("ok", "BOOLEAN")))
    assert not change.has_ddl and not change.kept_existing_type


def test_integer_to_decimal_is_widened():
    change = plan_schema_change(spec(Column("guests", "NUMERIC")), existing(ExistingColumn("guests", "INT64")))
    assert change.widened == [("guests", "INT64", "NUMERIC")]
    assert change.effective.column("guests").type == "NUMERIC"


def test_narrowing_keeps_wider_existing_column():
    change = plan_schema_change(spec(Column("price", "INT64")), existing(ExistingColumn("price", "NUMERIC")))
    assert change.effective.column("price").type == "NUMERIC" and not change.widened


def test_existing_string_column_absorbs_type_change():
    change = plan_schema_change(spec(Column("code", "INT64")), existing(ExistingColumn("code", "STRING")))
    assert change.effective.column("code").type == "STRING"
    assert change.kept_existing_type == [("code", "STRING", "INT64")]


def test_multiselect_into_existing_string_is_serialized():
    change = plan_schema_change(spec(Column("tags", "STRING", "REPEATED")), existing(ExistingColumn("tags", "STRING", "NULLABLE")))
    assert change.effective.column("tags").mode == "NULLABLE"


def test_incompatible_change_fails_before_touching_table():
    with pytest.raises(SchemaConflictError, match="no lossless conversion"):
        plan_schema_change(spec(Column("wedding_date", "BOOL")), existing(ExistingColumn("wedding_date", "DATE")))
    with pytest.raises(SchemaConflictError):
        plan_schema_change(spec(Column("tags", "INT64")), existing(ExistingColumn("tags", "DATE", "REPEATED")))
