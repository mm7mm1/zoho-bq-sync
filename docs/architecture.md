# Architecture

## Components

```mermaid
flowchart TB
  subgraph GCP
    SCH[Cloud Scheduler] -->|POST jobs/zoho-bq-sync:run| JOB
    subgraph JOB[Cloud Run Job - one task]
      CLI[cli.py] --> ENG[sync/engine.py]
      ENG --> MD[zoho/metadata.py]
      ENG --> EX[sync/extract.py] --> RC[zoho/records.py]
      MD --> HC[zoho/client.py]
      RC --> HC
      HC --> AUTH[zoho/auth.py]
      ENG --> SB[schema/builder.py + evolution.py]
      ENG --> TR[transform/normalize.py]
      ENG --> CP[sync/checkpoint.py]
      ENG --> WH[warehouse/bigquery.py] --> SQL[warehouse/sql.py]
    end
    SM[(Secret Manager)] -. file mount .-> AUTH
    WH --> STG[(zoho_crm_staging)]
    WH --> DW[(zoho_crm)]
    JOB --> LOG[Cloud Logging]
  end
  AUTH --> ZA[Zoho Accounts]
  HC --> ZC[Zoho CRM API v8]
```

## One module, one run

```mermaid
sequenceDiagram
  autonumber
  participant E as Engine
  participant Z as Zoho API v8
  participant B as BigQuery
  E->>Z: GET settings/fields?module=Wedding_Bookings
  E->>Z: GET settings/fields?module=Booked_Services (subform)
  E->>B: get_table schema
  E->>B: create table / ADD COLUMN / ALTER COLUMN SET DATA TYPE (additive DDL)
  E->>B: SELECT _sync_state (checkpoint)
  E->>Z: GET Wedding_Bookings?fields=Modified_Time&sort_by=Modified_Time (If-Modified-Since: checkpoint-overlap)
  loop per changed record (subform module)
    E->>Z: GET Wedding_Bookings/{id}
  end
  E->>Z: GET Wedding_Bookings/deleted?type=all (If-Modified-Since)
  E->>B: load jobs -> staging tables (parent, child, deleted)
  E->>B: BEGIN TRANSACTION, MERGE parent, MERGE child, soft-delete removed children, apply deletions + cascade, MERGE _sync_state, COMMIT
  E->>Z: GET Wedding_Bookings/actions/count
  E->>B: reconciliation counts, append _reconciliation and _sync_runs
```

## Data model

```mermaid
erDiagram
  wedding_bookings ||--o{ wedding_bookings_services : "parent_id = id"
  wedding_bookings {
    STRING id PK "Zoho record id"
    STRING name
    STRING ceremony_type
    DATE wedding_date
    NUMERIC package_price
    TIMESTAMP modified_time "version column"
    STRING owner_id
    STRING owner_name
    TIMESTAMP _synced_at
    STRING _run_id
    BOOL _is_deleted
    TIMESTAMP _deleted_at
    STRING _raw_json
  }
  wedding_bookings_services {
    STRING id PK "subform row id"
    STRING parent_id FK
    INT64 _row_index
    TIMESTAMP _parent_modified_time "version column"
    STRING service_name
    NUMERIC unit_price
    INT64 quantity
    BOOL _is_deleted
  }
  _sync_state {
    STRING module PK
    TIMESTAMP last_modified_time "checkpoint"
    TIMESTAMP last_deleted_check
    TIMESTAMP updated_at "last success"
  }
```

## Key design decisions

1. **The checkpoint commits in the same transaction as the data.** The pipeline cannot reach a
   state where the data was written but the checkpoint was not, or the reverse. A crash at any
   point means the next run re-reads the same window. MERGE is idempotent, so that is safe.
2. **Versioned MERGE.** The rule is `WHEN MATCHED AND S.modified_time >= T.modified_time`, so a
   replay or an overlapping window never overwrites newer data with older data. The same version
   may re-write a row, which is how new columns get backfilled, with one exception: a row
   soft-deleted after that source row was read stays deleted. Removed subform rows are stamped
   with the parent's new version for the same reason.
3. **Load jobs, not streaming inserts.** Rows in the streaming buffer cannot be updated by DML.
   Load jobs are free, atomic and schema-checked.
4. **Additive-only schema evolution, outside the transaction.** BigQuery does not allow DDL inside
   multi-statement transactions. Adding a NULLABLE column is harmless if the data step later fails.
   Type conflicts fail before any write.
5. **Soft deletes.** `_is_deleted` and `_deleted_at` keep history and make deletions auditable.
   Analysts filter with `_is_deleted IS NOT TRUE`. Hard purge, if required, is a separate job.
6. **Drift-safe checkpoint** ([sync/checkpoint.py](../src/zoho_bq_sync/sync/checkpoint.py)).
   Offset pagination over a live, time-sorted result set can skip a record that is edited during
   the run. The checkpoint only advances over pages served before the first concurrent edit.
7. **Module isolation.** Each module has its own transaction. One failing module does not block
   the others, but the run still exits non-zero, so the Cloud Run execution is marked failed.
8. **Explicit budget.** Zoho API credits are shared by the whole org for 24 hours. A runaway sync
   must not take down other integrations, so each run has a hard call cap.
9. **Warehouse interface.** The engine depends on a small protocol. BigQuery SQL is tested as
   strings and executed in the live test. Orchestration is tested against an in-memory model.

## Type mapping

| Zoho data_type | BigQuery | Notes |
|---|---|---|
| text, textarea, email, phone, website, picklist, autonumber | STRING | |
| integer, bigint | INT64 | |
| double, percent | FLOAT64 | |
| currency, decimal | NUMERIC | Loaded as a decimal string: no float rounding |
| boolean | BOOL | |
| date | DATE | |
| datetime | TIMESTAMP | Converted to UTC |
| lookup, ownerlookup, userlookup | `<f>_id` STRING + `<f>_name` STRING | |
| multiselectpicklist | REPEATED STRING | `UNNEST()` in queries |
| multiselectlookup, multiuserlookup, fileupload, imageupload, consent_lookup | STRING (JSON text) | `JSON_VALUE()` works on it |
| formula, rollup_summary | from `return_type` | |
| subform | child table | `<parent>_<subform>` or `TABLE_NAME_OVERRIDES` |
| unknown | via `json_type`, else JSON text | Logs `type_mapping_fallback` |

Column names come from the Zoho API name, lowercased, with non-`[a-z0-9_]` characters replaced.
A leading `_` is reserved for pipeline columns. Collisions are resolved deterministically by Zoho
field id order.
