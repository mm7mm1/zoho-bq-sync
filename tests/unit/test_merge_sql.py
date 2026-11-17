import re

import pytest

from zoho_bq_sync.schema.model import Column, TableSpec
from zoho_bq_sync.warehouse import sql

PARENT = TableSpec(
    name="wedding_bookings",
    zoho_module="Wedding_Bookings",
    columns=(Column("id", "STRING", "REQUIRED"), Column("name", "STRING"), Column("modified_time", "TIMESTAMP"), Column("_synced_at", "TIMESTAMP"), Column("_is_deleted", "BOOL")),
    version_column="modified_time",
)
CHILD = TableSpec(
    name="wedding_bookings_services",
    zoho_module="Booked_Services",
    columns=(Column("id", "STRING", "REQUIRED"), Column("parent_id", "STRING", "REQUIRED"), Column("_parent_modified_time", "TIMESTAMP"), Column("price", "NUMERIC")),
    version_column="_parent_modified_time",
    parent_key="parent_id",
    subform_field="Booked_Services",
)
T = "`demo-proj.zoho_crm.wedding_bookings`"
S = "`demo-proj.zoho_crm_staging.wedding_bookings__r1`"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_merge_upserts_by_id_with_version_guard():
    stmt = norm(sql.merge_upsert_sql(T, sql.dedup_source(S, "id", "modified_time"), PARENT.column_names, "id", "modified_time"))
    assert stmt.startswith(f"MERGE {T} AS T USING (SELECT * EXCEPT(_dedup_rn)")
    assert "ROW_NUMBER() OVER (PARTITION BY `id` ORDER BY `modified_time` DESC)" in stmt
    assert "ON T.`id` = S.`id`" in stmt
    assert (
        "WHEN MATCHED AND (T.`modified_time` IS NULL OR S.`modified_time` IS NULL OR S.`modified_time` > T.`modified_time` "
        "OR (S.`modified_time` = T.`modified_time` AND TRUE)) THEN UPDATE SET"
    ) in stmt  # PARENT has no _deleted_at column -> plain ">=" semantics
    update_set = stmt.split("THEN UPDATE SET", 1)[1].split("WHEN NOT MATCHED", 1)[0]
    assert "`name` = S.`name`" in update_set and "`id` = S.`id`" not in update_set  # key is never updated
    assert "WHEN NOT MATCHED BY TARGET THEN INSERT (`id`, `name`, `modified_time`, `_synced_at`, `_is_deleted`) VALUES (S.`id`, S.`name`" in stmt


def test_same_version_replay_cannot_revive_soft_deleted_row():
    cols = ["id", "modified_time", "_synced_at", "_is_deleted", "_deleted_at"]
    stmt = norm(sql.merge_upsert_sql(T, S, cols, "id", "modified_time"))
    assert "S.`modified_time` = T.`modified_time` AND (T._is_deleted IS NOT TRUE OR T._deleted_at IS NULL OR S._synced_at > T._deleted_at)" in stmt


def test_merge_without_version_has_no_guard():
    stmt = sql.merge_upsert_sql(T, S, ["id", "name"], "id", None)
    assert "WHEN MATCHED THEN UPDATE" in stmt


def test_child_reconciliation_only_touches_refetched_parents():
    stmt = norm(sql.soft_delete_removed_children_sql("`p.d.c`", "`p.s.c`", "`p.s.p`", "modified_time"))
    # The parent's new version is stamped on the deleted row, so an older replay cannot revive it.
    assert "SET _is_deleted = TRUE, _deleted_at = @run_ts, _run_id = @run_id, _parent_modified_time = COALESCE(P.version, T._parent_modified_time)" in stmt
    assert "FROM (SELECT id, MAX(`modified_time`) AS version FROM `p.s.p` GROUP BY id) AS P" in stmt
    assert "WHERE T.`parent_id` = P.id AND T._is_deleted IS NOT TRUE" in stmt
    assert "P.version >= T._parent_modified_time" in stmt
    assert "NOT EXISTS (SELECT 1 FROM `p.s.c` AS C WHERE C.id = T.id)" in stmt
    unversioned = norm(sql.soft_delete_removed_children_sql("`p.d.c`", "`p.s.c`", "`p.s.p`", None))
    assert "FROM (SELECT DISTINCT id FROM `p.s.p`) AS P" in unversioned and "_parent_modified_time =" not in unversioned


def test_deletions_are_grouped_and_guarded_against_restores():
    stmt = norm(sql.apply_deletions_sql(T, "`p.s.del`", "modified_time"))
    assert "FROM (SELECT id, MAX(deleted_time) AS deleted_time FROM `p.s.del` GROUP BY id) AS D" in stmt
    assert "T.`modified_time` <= D.deleted_time" in stmt
    cascade = norm(sql.cascade_child_deletions_sql("`p.d.c`", "`p.s.del`"))
    assert "WHERE T.`parent_id` = D.id AND T._is_deleted IS NOT TRUE" in cascade


def test_full_resync_soft_deletes_missing_rows():
    stmt = norm(sql.soft_delete_missing_sql(T, S))
    assert f"UPDATE {T} AS T" in stmt and "NOT EXISTS (SELECT 1 FROM " + S + " AS S WHERE S.`id` = T.`id`)" in stmt


def test_transaction_wraps_statements_and_reraises():
    script = sql.transaction_script(["SELECT 1", "SELECT 2;"])
    assert script.startswith("BEGIN\nBEGIN TRANSACTION;")
    assert "SELECT 1;\n\nSELECT 2;" in script
    assert "COMMIT TRANSACTION;\nEXCEPTION WHEN ERROR THEN\n  ROLLBACK TRANSACTION;\n  RAISE USING MESSAGE = @@error.message;\nEND;" in script


def test_apply_statement_order_and_state_last():
    statements = sql.build_apply_statements(
        parent_spec=PARENT,
        parent_target=T,
        parent_staging=S,
        children=[sql.StagedChild(CHILD, "`p.d.c`", "`p.s.c`")],
        deleted_staging="`p.s.del`",
        soft_delete_missing=False,
        state_table="`p.d._sync_state`",
    )
    kinds = [s.split()[0] + ":" + s.split()[1] for s in statements]
    assert kinds == [
        f"MERGE:{T}",  # parent upsert
        "MERGE:`p.d.c`",  # child upsert
        "UPDATE:`p.d.c`",  # removed subform rows
        f"UPDATE:{T}",  # deleted records
        "UPDATE:`p.d.c`",  # cascade to children
        "MERGE:`p.d._sync_state`",  # checkpoint commits with the data
    ]


def test_state_only_statement_when_nothing_changed():
    statements = sql.build_apply_statements(
        parent_spec=PARENT, parent_target=T, parent_staging=None, children=[sql.StagedChild(CHILD, "`p.d.c`", None)],
        deleted_staging=None, soft_delete_missing=False, state_table="`p.d._sync_state`",
    )
    assert len(statements) == 1 and "@checkpoint AS last_modified_time" in statements[0]


def test_identifiers_are_validated():
    with pytest.raises(ValueError):
        sql.q("bad`name")
    with pytest.raises(ValueError):
        sql.table_ref("proj", "ds", "t; DROP")
