# Local 40-day Posts collection

This entry point stages captured public post metadata. It does not run discovery,
upserts, Gemini, OpenAI, S3, Event Master, Recovery, or Dashboard. Existing paid
ledgers, attempt journals, import markers and database IDs are untouched. There
is no AI/production handoff. Image field validation is not source verification.

Read-only preflight (no browser or staging writes):

```bash
cd /home/zoewu/cssa/instagram/playwright-Playground
./venv/bin/python collect_instagram_posts.py --profile dvcatucsd --mode initial --preflight
```

Explicit initial collection, one headless profile visit, no automatic retries:

```bash
./venv/bin/python collect_instagram_posts.py --profile dvcatucsd --mode initial --run
```

Manual incremental collection, when separately authorized:

```bash
./venv/bin/python collect_instagram_posts.py --profile dvcatucsd --mode daily --run
```

Both modes retain a rolling **40 x 24-hour window** before the aware run instant;
run dates and same-day daily suppression use America/Los_Angeles (including DST).
The exact cutoff instant is inclusive. Missing, conflicting, invalid or future
publication timestamps (>5 minutes ahead) are quarantined, not active candidates.
Old observations are counted and excluded, never inserted into PostgreSQL. Prior
staging objects are retained; active_recent.json rolls forward to exclude old ones.
A successfully completed initial collection also suppresses daily collection on
that LA date. Failed/interrupted runs retain partial artifacts and are not marked
complete. Nothing retries them automatically.

Maximum 20 scrolls and 300 seconds total, including browser cleanup. A separate
supervisor interrupts, then kills the worker process group if necessary. Browser
navigation has a 60-second timeout. Bounds may only be reduced using
--max-scrolls / --max-seconds. The existing pinned-aware chronological rule is
available only behind independently verified ordering; currently no producer
establishes that evidence. Therefore old posts alone never stop live scrolling.
A bounded completion does NOT claim complete timeline coverage.

Staging is `.instagram-post-staging/` (git ignored, owner-only directories 0700,
files 0600):

- `objects/<sha256>.json`: versioned media-only snapshots, including captions,
  image references and collaboration hints. No full response envelopes, request
  bodies, cookies, headers or authentication fields. Credentials are filtered.
- `runs/<run-id>.recent.jsonl`: references to recent candidate observations,
  fixed request provenance classifications and unresolved validation blockers.
- `runs/<run-id>.quarantine.jsonl`: unknown timestamps or identity conflicts.
- `runs/<run-id>.summary.json`: aggregate checkpoint then final outcome, including
  strict production scrape health. Partial response collection never changes that
  health result to healthy.
- `state.json`, `active_recent.json`: deduplication/index state, with AI eligibility
  always false. Same shortcode plus conflicting identities/authors is quarantined,
  never merged. Supported numeric canonical identities deduplicate across runs;
  unsupported compound IDs use only an EXACT observed identity tuple for staging
  deduplication. That tuple is NOT a verified canonical identity or import key.
- `run.lock`: existing nonblocking flock implementation reused for this collector.

Every observed metadata version is preserved even when a candidate is a duplicate.
No captions/image similarity merging. Staging counts are candidate counts, not
verified eligible posts. `single_image_fields` / `image_only_carousel_fields`
indicate explicit media types, usable references and complete child counts only;
identity, source, GraphQL integrity and screening blockers remain separate. Videos,
Reels, mixed media, unknown types and incomplete carousels cannot advance to AI.
Unmatched authors/coauthor hints never become accepted collaborations.

Protect and back up this private directory; it includes public post metadata and
potentially expiring CDN URLs. Never commit, serve publicly, or paste objects into
logs. No automatic deletion/retention cleanup is implemented. Archive old runs
privately; preserve index/objects together. 2 MB per media snapshot, 64 MB newly
written objects per run, and 5000 candidate records are fail-closed limits. Object
versions accumulate across runs; monitor disk space. stdout is aggregate JSON;
unstructured browser output and stderr are suppressed. Summaries intentionally
contain no source URLs, usernames beyond the configured target, or raw errors.

`deploy/instagram-collection/*.service` / `*.timer` are UNINSTALLED drafts for
03:00 America/Los_Angeles, Persistent=true, no service restart. They contain an
explicit false activation flag. Additionally --scheduled refuses execution until
the source/identity/integrity and verified production handoff are reviewed in code.
Changing an environment flag alone cannot bypass this gate. No scheduler command
is appropriate yet; do not install/enable these drafts. Systemd availability,
authenticated cookies/browser dependencies, and successful production handoff
must all be verified before activation. This collector does not replace existing
paid-run locks or grant permission to spend daily budgets.
