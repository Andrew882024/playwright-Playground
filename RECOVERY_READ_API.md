# Private recovery reads and service authentication

GET-only private endpoints:

- `/internal/recovery/health`
- `/internal/recovery/configuration`
- `/internal/recovery/tasks?page=1&page_size=25&status=failed&stage=cover`
- `/internal/recovery/parents/{parent_id}`
- `/internal/recovery/parents/{parent_id}/import-preview`

Require `Authorization: Bearer <PLAYGROUND_RECOVERY_READ_TOKEN>` on every request.
Provision an independent random 32–512 character read token in both Playground
and Event Master deployment secrets. Keep the origin private and serve HTTPS in
production. Never expose the token to Next.js/browser environment variables.

Existing HTTP `/sync/fomo` and `/sync/instagram_pipeline` require a DIFFERENT
`PLAYGROUND_SYNC_TOKEN` with the same minimum length and Bearer scheme. Missing
or weak config fails closed. Reusing the read token for sync fails closed. Update
existing authorized HTTP callers to send this sync credential; their successful
pipeline execution logic remains unchanged. Local operator CLIs do not use HTTP
and remain unchanged. Sync HTTP failures are sanitized; FOMO no longer returns
its ingest URL or raw remote error text.

The read API never invokes Store.candidates/claim/finish, recovery.preview,
acknowledge, subprocesses, pipeline operations, file locks, PNG recovery, signing,
or AI/S3 calls. It uses SELECTs under read-only repeatable-read transactions and
always rolls back. Read endpoints neither insert task rows nor update attempts,
leases, scan timestamps or acknowledgments. It has no mutation endpoints.

Task pagination is bounded (page<=10000, page_size<=100); filters are fixed
status/stage enums. Tasks list existing recovery rows. Parent ID inspection is
available independently for untracked posts. Current acknowledgment is recomputed
from committed parent/child revisions without writing it back. There is no
recovery action hidden behind a GET.

Parent/image output is DTO-only: escaped display text, activity summaries, image
presence/source and hashes; no raw signed URLs or manifests. Import previews use
the pure `instagram_event_master_projection` module shared with the real backend
importer, returning hashes of owned fields and provenance. Event Master performs
its own SELECT-only target/legacy/dependency verification. Reading a preview does
not launch the importer or the acknowledgment verifier subprocess.

Existing DB_* settings are used; no real .env file was modified. No migration was
created or run. Parent/recovery schema must already exist; missing schema returns
fixed unavailable status. Health reports recovery-table availability. No service
configuration secrets are included in the configuration response, only explicit
pipeline flags and numeric caps. Responses are no-store.

Offline API tests mount routers only (not app.main), mock sessions and pipeline
functions, and never start application startup create_all, real pipeline work or
external services. Event Master and Playground must deploy the shared projection
change together before resuming normal imports. This phase includes no frontend.
