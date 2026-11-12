# Discovery execution records

Every `run_instagram_discovery_pipeline.py --run` creates an audit record before
loading the pipeline or opening the browser. Preflight remains read-only and
creates no execution files. A failed optional dependency import is recorded too,
including a missing `python-dotenv` dependency (using the default/process-env log
directory if `.env` cannot be loaded).

Default directory: `~/.local/state/event-master/instagram-discovery-runs`.
Override with `INSTAGRAM_DISCOVERY_LOG_DIR`, an absolute persistent local directory.
Use the same OS user/directory for unattended runs. Keep it outside the checkout,
temporary directories, container layers, and cache-cleanup targets. No database
migration is needed. This does not enable Recovery, Dashboard, or scheduling.

Each UUID run has `<run_id>.json` and `<run_id>.lock`. Records are written with
private file permissions using temporary files, atomic replacement, and fsync of
files/directories. The initial record has `status="running"` and no end timestamp.
Stage checkpoints retain safe counters/timing and parent IDs. Parent-stage
outcomes are filled when the parent runner returns; `awaiting_outcome` means the
stage boundary was passed but its result has not yet been reported. It must not
be interpreted as success.

Stage names are `scraping`, `screening`, `gemini`, `original_image`, `cover`, and
`event_master`. Records contain fixed reason codes, numeric HTTP codes, selected
safe booleans, and daily Gemini/cover budget usage/remaining allowance. They never
copy captions, prompts, URLs, cookies, credentials, provider bodies, subprocess
output, exception messages, or tracebacks. Unknown reasons become `other`.
Unstructured pipeline console output is discarded; the CLI prints its final JSON
to stdout and a concise safe summary to stderr. The record also stores that summary.

Scrape-health diagnostics include `error_categories` per profile and aggregated
across profiles. The durable scraping-stage item retains these as
`capture_error_categories`, copying only known category names and nonnegative
integer counts. Categories distinguish `response_body_unavailable`,
`json_decoding_failure`, `unexpected_json_structure`, `missing_timeline_edges`,
`unsupported_timeline_schema`, `browser_context_closed`, and
`response_processing_exception`, plus `http_error`, `graphql_error`, and
`request_failed`. These count error events, including listener-cleanup failures;
they need not equal the number of HTTP responses. Their sum equals `capture_errors`.

An explicit supported timeline connection with `edges: []` counts as
`valid_empty_timeline_responses`. Missing/null/malformed edges, unsupported nested
connections, and changed node schemas never establish an empty timeline. Unrelated
GraphQL responses increment `ignored_graphql_responses`; they cannot establish a
successful scrape. Any capture error still makes the scrape unhealthy, even if
another response was valid. No additional Instagram schemas or JSON prefixes are
automatically accepted. Headed/headless modes share the same capture/parser code.

Additional privacy projections appear per profile, in aggregate scrape health,
and on the durable scraping-stage item:

- `graphql_error_diagnostics`: response/error-object counts, fixed `code_counts`,
  and aggregated `structures`. Codes are read only from `code`, `error_code`, or
  `api_error_code` on an error object or its direct `extensions` object. Only
  integer values in `SAFE_ERROR_CODES` in `graphql_diagnostics.py` are emitted;
  strings, booleans, unknown numbers, IDs, and arbitrary extension values are not.
  Codes are counted once per error object, even if repeated in multiple fields.
  Subcode values are never emitted. Structural flags report presence of known
  fields (message, path, locations, extensions, summary, etc.), not their contents.
  `container` is list/object/other; `item_kind` is object/other. Unknown numeric or
  nonnumeric codes produce boolean flags only. At most 128 error items per
  response are inspected; `omitted_error_items` reports the remainder.
- `unsupported_timeline_shapes`: aggregated fixed boolean patterns plus `count`
  and `shape_kind` (top_level_edges/nested_edges/alternate_nodes/post_like_other/
  unknown). Indicators show whether data exists/is an object; edges exist/are
  lists; node/item/media containers are nested beyond the supported edge/node
  placement; those containers have code/shortcode/id fields; and known pagination
  fields exist. No field values, cursor values, arbitrary keys or JSON paths are
  retained. Shape inspection is bounded to 4096 visited JSON entries and depth 24; a partial
  inspection sets `inspection_truncated`.

These projections never determine acceptance or add parsing support. Only
unsupported responses receive schema-pattern records; supported/empty responses
continue using existing validity counters. The same strict allowlist is applied
again before durable logging. The historical runs cannot be backfilled with these
details without another separately authorized observation.

For the next separately authorized diagnostic, `timeline_wrapper_diagnostics`
adds fixed-position patterns: flat_node, node_media, node_media_or_ad,
node_media_or_ad_media, plus explicit malformed/empty/unknown cases. These are
candidate wrapper labels, NOT newly supported parser schemas. Each pattern shows
top_level/under_user/unknown placement, a known-timeline/other-or-unknown root
enum, and boolean presence/type indicators for identity, timestamps, caption,
image fields, and pin metadata. Neither connection names nor field values are
recorded. Inspection is capped at 64 connections and 128 edges per response;
`omitted_items` exposes truncation. Counts reflect inspected candidates, so one
edge can contribute a flat pattern and a nested-media pattern.

`graphql_error_diagnostics.path_classifications` aggregates fixed categories for
identity, time, media, caption, organizer, pagination, connection, optional-field
candidates, unknown/mixed, and missing/malformed paths. It records only booleans
and enums, never raw paths, index values, or arbitrary field names. Known roots
are exact allowlisted literals already used in code/tests; unknown roots remain
unknown. Paths are bounded to 32 segments. Optional candidates are limited to
unused viewer/like leaf fields or explicitly named optional-root candidates;
comment metadata is NOT considered optional because the scraper reads it.

Attribution does not establish that a partial GraphQL response contains all
required post data. Optional-looking paths are not proof that an error is safe
to ignore, and all error responses continue to fail closed. No saved positive
Instagram wrapper fixture was found during the offline investigation; existing
nested fixtures are synthetic negative tests. The timeline extractor/classifier
remain unchanged until a specific live wrapper is demonstrated by these bounded
indicators and then covered by an explicit parser regression fixture.

`paid_requests_started` is explicitly checkpointed at the provider dispatch
boundary. It is conservative: interruption between that checkpoint and network
dispatch can leave a possible request counted. It does not establish billing or
provider receipt. Existing request journals, cover manifests and daily reservations
remain the authority for retry safety. Logging never retries a request.

## Monitoring contract

| Record status | Monitoring action |
| --- | --- |
| `completed` | Successful, including legitimate empty discovery/budget skips. |
| `completed_with_review_required` | Human review queue; CLI may still exit 0. |
| `failed_before_paid_requests` | Investigate scrape/authentication/dependency/database failure; no dispatch boundary recorded. |
| `failed_after_paid_request` | Inspect journals/manifests before authorizing anything; never restart as a paid retry. |
| `interrupted` / `timed_out` | Incomplete execution; paid counters and journals need review. |
| `busy` | Existing pipeline lock prevented work. |
| `running` with no end | May be live or unfinished; check its lock. |

JSON stdout adds `execution.run_id`, `execution.status`, and `execution.summary`.
The original pipeline status remains available; the record stores it separately as
`pipeline_status`. Existing CLI success/failure exit behavior is preserved.
Budget exhaustion is a skip, not a failed provider request. A genuine provider
failure remains a failure even if it also requires review.

A run holds an exclusive file lock for its lifetime. The next execution records
`active_run_ids` (lock still held), `previous_unfinished_run_ids` (running record
whose lock is free), and the count of unreadable prior records. Earlier records
are never rewritten or automatically recovered. A monitoring process may use a
nonblocking `flock` on the existing `.lock` file to make the same distinction.
Do not use timestamp age alone to assume a process is dead.

SIGTERM and KeyboardInterrupt finalize as interrupted when Python can handle them.
An escaped TimeoutError finalizes as timed out. SIGKILL, power loss, or a blocked
runtime may leave an unfinished record for the next run to detect. No execution
deadline is added here; a future systemd RuntimeMaxSec can send SIGTERM. A generic
SIGTERM cannot prove it came from a timeout, so it records `termination_signal`.
Network timeouts already handled inside a stage retain that stage's failure reason.

Before a future timer is installed, configure its service to retain stdout/stderr,
run under the intended OS user, use the absolute venv interpreter/working directory,
and avoid automatic paid-job restarts. A monitor should read final record statuses,
review counts, budget blocking reasons, unreadable records, and unfinished IDs;
process exit code alone does not identify the review queue. No scheduler is
configured by this implementation.

## Failure, retention, and backup

If the initial record cannot be written, execution stops before scraping. If a
later checkpoint fails, the CLI reports `execution_log_unavailable` and preserves
the last durable record. New paid dispatch boundaries fail closed. A disk failure
can prevent finalization; monitor both CLI failure output and unfinished records.

No rotation/deletion is automatic. Retain at least 90 days of finalized records
for the pilot; archive older finalized JSON/lock pairs only after confirming their
locks are free. Retain unfinished/interrupted failures until reviewed. Never
unlink a live lock file. Back up records to durable storage while jobs are stopped
or using a filesystem snapshot. This log directory is independent from the daily
budget ledger, Gemini journals, and cover cache: never apply log retention to
those safety records. Loss of the complete log directory cannot be detected from
that directory alone; independent monitoring/backups are necessary.
