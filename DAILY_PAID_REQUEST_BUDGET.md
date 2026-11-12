# Daily paid-request safety

`run_instagram_discovery_pipeline.py` and `run_instagram_parent_pipeline.py`
share a ledger at `~/.local/state/event-master/instagram-paid-requests` by default.
`INSTAGRAM_PAID_REQUEST_LEDGER_DIR` may select another absolute persistent local
directory. Every scheduled/manual invocation must use the same OS user, directory,
and configuration. Do not put it in `/tmp`, the repository, a disposable container,
or a cleanup/cache directory. Mount persistent storage when using containers.

Defaults are `INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS=1` and
`INSTAGRAM_DAILY_COVER_MAX_REQUESTS=1`. Zero prohibits new paid requests. These are
shared request limits, not dollar/token limits. Existing per-run caps still apply.
An initialized day's limit can only decrease; raising configuration does not grant
more requests that day. The next day uses the configured limits.

Reservations use America/Los_Angeles calendar dates at dispatch reservation time,
including 23/25-hour DST days. A stable `ledger.lock` serializes processes (bounded
two-second lock acquisition). `ledger.json` holds all days and reservation IDs,
parent IDs, provider buckets and UTC timestamps; it contains no prompts, images,
credentials, URLs, or API responses. Atomic replacement and file/directory fsync
finish before request intent and API dispatch. No reservation is refunded, even
after failure, timeout, interruption, or a crash between reservation and dispatch.
The journal/manifests remain responsible for request outcomes and manual review.

A denied Gemini reservation creates no parent request intent and no analyzed row.
A denied cover reservation leaves a prepared manifest resumable. Generated PNG
reuse, upload recovery, and already-published covers need no reservation. Manual
retry authorization cannot bypass the daily limit. Crossing midnight also does
not bypass a parent's existing uncertain-request protection.

Preflight reads usage without creating files. Missing initial state reports zero;
corrupt/inaccessible state fails closed. A lock file without its ledger is treated
as lost/interrupted state, never initialized again automatically. Investigate it
manually rather than deleting the lock. Unavailable storage prevents new paid
requests and is reported using fixed safe diagnostics.

Back up the entire directory along with parent attempt journals and cover
manifests/PNGs. Take a consistent snapshot while jobs are stopped (or holding the
same lock), retaining permissions. Never replace/unlink the live lock inode while
jobs run. No automatic pruning is implemented: retain the full ledger throughout
the pilot. Archive backups without removing live days. Never restore an older
ledger and immediately resume spending; reconcile provider usage and uncertain
reservations first. Loss of the entire directory cannot be detected automatically.

This is single-host/local-filesystem coordination, not a distributed ledger.
Different directories/users/hosts, direct provider calls, and legacy analyzer or
cover CLIs outside these two runners are outside its scope. Do not schedule those
as alternatives. Changing system time can change the calendar day; use a reliable
host clock. No existing historical paid requests are backfilled by this change.

Execution/preflight JSON includes `daily_budget` with the LA day, per-provider
`limit`, `used`, `remaining`, and a safe blocking reason. Budget exhaustion is an
expected skip; unreadable/unwritable ledger state requires review. A blocked cover
does not prevent the existing Event Master fallback import.
