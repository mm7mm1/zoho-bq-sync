# Zoho CRM API v8: what the pipeline uses and its limits

Base URL: `{api_domain}/crm/v8`. Auth header: `Authorization: Zoho-oauthtoken <access_token>`.

## Endpoints

| Purpose | Endpoint | Notes |
|---|---|---|
| Access token | `POST {accounts}/oauth/v2/token` `grant_type=refresh_token` | 1 h token. Max 10 tokens per refresh token per 10 min. Errors can come back as HTTP 200 with an `error` key. |
| Modules | `GET /settings/modules` | `generated_type`: default, custom, subform, linking, web. Only default and custom with `api_supported=true` are synced. |
| Fields | `GET /settings/fields?module={api}` | `data_type`, `json_type`, `formula.return_type`. Subform fields carry `associated_module.module`. |
| Changed records | `GET /{module}?fields=..&sort_by=Modified_Time&sort_order=asc&per_page=200` + `If-Modified-Since` | `fields` is mandatory (max 50). `page` reaches 2,000 records, then `page_token` is needed. 304 means nothing changed. |
| Extra field chunks | `GET /{module}?ids=..&fields=..` | Max 100 ids per call |
| Single record | `GET /{module}/{id}` | The only call that returns **subform rows** and multi-select or multi-user lookup values |
| Deleted records | `GET /{module}/deleted?type=all` + `If-Modified-Since` | Recycle-bin entries for 60 days, permanently deleted for 120 days |
| Record count | `GET /{module}/actions/count` | Used for reconciliation |

References: [Get Records](https://www.zoho.com/crm/developer/docs/api/v8/get-records.html),
[Deleted Records](https://www.zoho.com/crm/developer/docs/api/v8/get-deleted-records.html),
[Record Count](https://www.zoho.com/crm/developer/docs/api/v8/module_record_count.html),
[Field Metadata](https://www.zoho.com/crm/developer/docs/api/v8/field-meta.html),
[API Limits](https://www.zoho.com/crm/developer/docs/api/v8/api-limits.html),
[Status codes](https://www.zoho.com/crm/developer/docs/api/v8/status-codes.html).

## Change detection

- `If-Modified-Since: <checkpoint - SYNC_OVERLAP_SECONDS>` together with `sort_by=Modified_Time asc`.
- The checkpoint is the max `Modified_Time` of consistently read pages (see the drift note below).
  It is never the wall-clock time, so a clock skew between GCP and Zoho cannot lose records.
- The overlap (default 300 s) covers the one-second timestamp resolution and the unclear
  inclusive/exclusive semantics of `If-Modified-Since`. Duplicates are harmless because of MERGE.
- **Pagination drift.** Offsets shift when a record is edited mid-run, and a record can be
  skipped. The pipeline records Zoho's `Date` header per page. It only advances the checkpoint
  over pages served before the first record whose `Modified_Time` falls after extraction started.
  It logs `pagination_drift_detected` when that happens.
- **Subforms.** The list API returns only a count for a subform. Modules with subforms (or
  multi-lookups) are listed as `id + Modified_Time`, then each changed record is fetched with
  `GET /{module}/{id}`. That costs 1 credit per changed record. For high-volume modules, see the
  alternatives below.
- **Assumption to verify in your org:** editing only a subform row updates the parent record's
  `Modified_Time`. Zoho's UI and Records API save the subform as part of the parent record, so
  the demo expects that. Step 8 of the demo verifies it live. If a workflow or integration
  writes subform rows without touching the parent, run a periodic `sync --full`.

## Deletion detection

| Kind | Mechanism |
|---|---|
| Record deleted in Zoho | Deleted-records API every incremental run. The record is soft-deleted, its subform rows are cascaded, and a restored-then-edited record is protected by a guard. |
| Subform row removed | The parent is re-fetched in full. Its active child rows missing from the new array are soft-deleted. |
| Deletion older than retention, or missed | `sync --full` soft-deletes every row absent from Zoho. The run is blocked if more than `MAX_DELETE_FRACTION` of active rows would be deleted. The step is skipped if pagination drift was detected. |
| Restored from recycle bin | Picked up if the restore changes `Modified_Time`. Otherwise the next `--full` fixes it. |

## Rate limits and credits

- Credits are a rolling 24 h allowance per org, based on edition and licences. A list call costs
  1 credit per request. Insert and update calls cost 1 credit per 10 records.
- Concurrency is limited per app per edition (Free 5 to Ultimate 25). Sub-concurrency for heavy
  APIs is 10. The pipeline is sequential, so its concurrency is 1.
- HTTP 429 means credits are exhausted **or** a concurrency limit was hit. The client honours
  `Retry-After`, otherwise it uses exponential backoff with jitter. It then fails loudly instead
  of sleeping for hours when credits are gone.
- `X-API-CREDITS-REMAINING` is sent only after 50% of the daily credits are used. It is logged
  as `zoho_credits_low`.
- `ZOHO_MAX_API_CALLS_PER_RUN` is a hard stop, so one bad run cannot starve other integrations.
- Writes (seed script) are not retried on 5xx or timeouts, which could duplicate records.
  They are retried only on 429, which means the request was rejected before processing.

## Known limitations and alternatives (not implemented)

- `page_token` reaches at most 100,000 records per query. Initial loads bigger than that should
  use the **Bulk Read API** (async CSV export jobs). It is cheaper in credits for large volumes.
- Per-record detail fetch for subform modules does not scale to hundreds of thousands of daily
  changes. Options: Bulk Read on the subform module, COQL on the subform API name (verify that
  your edition supports it), or the Subforms API (Enterprise and Ultimate only).
- Near-real-time sync would use the Notifications (Watch) API or workflow webhooks to a Cloud Run
  service. Those signals still need this batch path as the source of truth.
- Field history (who changed what and when) is not replicated. `_raw_json` only keeps the latest
  version.
- Picklist label renames change stored values from that point on. Historic rows keep the old label.
- A custom module or subform is created in the Zoho UI (see [zoho_setup.md](zoho_setup.md)).
  Zoho has a Create Custom Fields API, but the demo adds the field in the UI on purpose, so the
  audience sees that no code is involved.
