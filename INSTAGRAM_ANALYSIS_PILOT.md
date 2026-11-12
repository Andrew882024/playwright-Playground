# Prepared staged event-analysis pilot (no paid dispatch)

Use existing staged originals/captions as UNTRUSTED source material for content
classification. Permission to analyze is independent from source authenticity and
permission to import. No source, compound-ID, collaboration or GraphQL blocker is
cleared by preparing this batch or receiving an eventual AI result.

```bash
./venv/bin/python prepare_instagram_analysis_batch.py --limit 5
./venv/bin/python prepare_instagram_analysis_batch.py --limit 5 --save
```

There is deliberately no --run option. Default preparation is read-only; --save
writes only a private 0600 manifest under .instagram-post-staging/analysis-batches.
The command cannot call Gemini/OpenAI, upload, connect to a database, reconcile
children, import events, schedule, reserve budget or modify a request journal.
It uses the existing daily ledger's read-only summary and existing parent-journal
inspect function. No credentials, captions, image URLs or prompts enter stdout or
the review manifest. Exact source objects stay private and immutable by hash.

## Selection

Build the existing review queue, retain recent valid-publication, image-only rows
with usable captions and confidently future local caption dates. Skip imported,
already-analyzed, expired, unsupported/ambiguous media, legacy/identity conflicts,
missing-date and short/invalid-caption candidates. Validate staged object hashes.
Deduplicate exact shortcodes across discovery accounts; prefer the observed
primary-author account for that SAME post without treating that preference as
source authentication. Prioritize recency, with one candidate per account before
additional posts from a repeated account. Keep all uncertainty reasons and
related discovery profiles in each selected record. Existing uncertain paid
attempts cannot become new analysis work; retry authorization is separate.

2026-11-05 preparation found four unique qualifying posts across three accounts:

1. rco.ucsd / DfXtAoxPf3A (published Nov 5; caption date candidate Nov 10).
2. tritongamingsd / DfVXqBoyVlv (Nov 4; date Nov 13), also observed through
   vgdc.at.ucsd. This is ONE analysis candidate, not two; collaboration unverified.
3. envisionucsd / DfP4e20v8Dm (Nov 2; date Nov 9); existing pending parent 1645.
4. rco.ucsd / DfVK8aHv261 (Nov 4; date Nov 9).

These are date candidates, not image-confirmed events. All have one recoverable
original image and nonempty caption (228, 833, 583, 301 characters respectively).
No image was fetched or checked for current accessibility. Do not replace failed
original downloads with generated covers. Revalidate dates and reference
availability immediately before paid work; stop BEFORE dispatch if the required
first original image cannot be loaded.

Original input is generated in the EXISTING analyze_post dictionary contract:
ordered openable_media_urls.largest, first_image_url, caption, publication reference,
shortcode/permalink. Single-image/child-array ordering is established within the
supplied metadata, NOT Instagram origin. image_order_verified only describes that
ordering; source_authenticity_verified remains false. Existing Gemini prompts,
models, evidence policy and events[] schema handle the actual image/text analysis;
there is no second event detector or model API implementation here.

## First approval and technical handoff

The saved manifest marks analysis_authorized=false and import_authorized=false.
No approval has been granted. Proposed first approval scope, after offline verification of
the handoff below:

"Analyze ONLY batch 52247697aaec4e09d9ae3b2b729f53679a27878193a0025bcdda447ca7bfe0e7,
candidate 3, envisionucsd / DfP4e20v8Dm (parent 1645). At most ONE Gemini HTTP
generation request total. Save results locally only. No automatic paid retry,
production database writes, S3/backfill, covers, imports or scheduling. Source
and import permissions remain unapproved."

Use the actual current batch hash/revision if preparation changes. Before any
execution: recheck exact object/input hash and parent/shortcode mapping, current
analyzed/import state, pending journal, expiry, and daily allowance under existing
locks. Absence from the dated snapshot is NOT authoritative proof of no import.

The ANALYSIS-ONLY handoff is now implemented in `analysis_execution.py`:

- Reuse gemini_analysis.analyze_post, existing model/config/client behavior and
  0..N events contract; do not call serial process_profile_db/persist_analysis or
  parent_pipeline.service.run (the latter also performs original-image backfill).
- Bind the approved staged input to existing parent 1645, with the existing parent
  advisory lock, attempt_safety.Attempt/capture, GeminiHttpBudget(1)/capture and
  daily_budget.Ledger/capture. SDK retry_options.attempts=1; budget covers retries,
  model fallbacks and JSON repair, not just post attempts.
- Ensure original-image load succeeds before paid dispatch, without silently
  falling back to text-only analysis. Reuse the existing image retrieval path.
- Save PostAnalysisAI output locally and atomically against the exact approved
  input revision. A successful local result is NOT committed PostgreSQL extraction:
  do not mark its parent journal committed. Keep noncommitted requests protected
  from automatic repeat, and reuse the local result without new payment later.
- Carry source uncertainty beside the result; Gemini cannot authenticate origin.
  Production reconciliation, covers and imports need separate evidence/approval.

The other three posts have no parent binding in the current read-only snapshot.
First perform an authorized authoritative lookup; if genuinely new, an explicitly
authorized canonical parent allocation/ingestion step is needed for reuse of the
existing positive-parent-ID ledger/journal. Do NOT invent temporary parent IDs or
create a separate incompatible paid journal in this preparation phase.

Estimate: up to one generation request per candidate, four for the whole batch.
First pilot is capped globally at one. The inspected daily allowance is one per
LA day, with one remaining at preparation; all four are not authorized to run
in one day. Preserve existing limits. Failures/503/timeouts/uncertain requests
remain counted and protected; do not assume a failed request is free.

None of the source records is ready for production import. No NITRO cover,
parent marker, Event Master record, environment setting or protected ledger/journal
was modified by preparation. Automated recovery and scheduling remain untouched.

## Analysis-only command (separate execution approval required)

Read-only authoritative PostgreSQL preflight, no image fetch/API/journal write:

```bash
./venv/bin/python analyze_instagram_batch.py \
  --batch-id 52247697aaec4e09d9ae3b2b729f53679a27878193a0025bcdda447ca7bfe0e7 \
  --parent-id 1645 --preflight
```

Only after separately approving one possibly billable HTTP request:

```bash
./venv/bin/python analyze_instagram_batch.py \
  --batch-id 52247697aaec4e09d9ae3b2b729f53679a27878193a0025bcdda447ca7bfe0e7 \
  --parent-id 1645 --run --confirm-one-paid-request
```

Execution holds the existing parent advisory lock through journal finalization.
Fresh READ ONLY PostgreSQL transactions check exact canonical shortcode/account,
publication/caption revision, analyzed status, existing children, cross-profile
parent ambiguity and local successful import acknowledgments. There is no MySQL
lookup: analysis permission does not grant import permission or guarantee absence
of a MySQL-only import. Any canonical conflict or unavailable state blocks payment.
These checks, staged hashes, journal and daily allowance are rechecked after image
download immediately before the shared dispatch reservation. Per-run HTTP limit
is fixed at one; SDK retries are disabled. No retry-authorization bypass is exposed.
Model selection and prompts remain the existing configuration; strict dispatch
controls prohibit a second fallback/repair request.

Creates private local `analysis-results/<batch-id>/1645/intent.json` before network
work, and atomically saves `result.json` only after valid Gemini extraction.
The result contains the existing events[] JSON, input/source hashes and uncertainty
flags; it can contain sensitive source references from the existing AI schema.
Keep the git-ignored staging directory private and back it up appropriately.
Raw caption and image references remain attached through the immutable source
object and exact analyzer-input hash, not printed to the terminal. Terminal JSON
contains safe diagnostics, output path, HTTP count, daily usage and false
source/import/production-commit flags.

A successful local analysis leaves the shared parent journal review-required,
NOT PostgreSQL-committed. Repeating the same command reuses the saved result free.
If a paid request fails, is interrupted, or output persistence fails, the durable
intent remains protected and subsequent automatic payment is blocked. Review and
recovery of any result/journal discrepancy must be explicit; do not delete/reset
records. The command does not run backfill, cover, import, recovery or scheduling.

## Current review-to-parent handoff (three pending candidates)

Parent 1645's authorized request failed and remains journal-blocked. The earlier
1645 commands above document the historical pilot; do not retry it automatically.
The current batch is:
`1b9218419f964e567c394525dbd091b62f7577edea265871111a519d52f782ca`.
Candidates 1 and 3 are rco.ucsd posts; candidate 2 is tritongamingsd.
Read-only PostgreSQL lookup found no existing parent for any of the three.

```bash
./venv/bin/python bind_instagram_analysis_batch.py \
  --batch-id 1b9218419f964e567c394525dbd091b62f7577edea265871111a519d52f782ca \
  --dry-run
```

The next separately approved operation creates/binds ONLY candidate 1; it makes
no Gemini request and grants no cover/import permission:

```bash
./venv/bin/python bind_instagram_analysis_batch.py \
  --batch-id 1b9218419f964e567c394525dbd091b62f7577edea265871111a519d52f782ca \
  --candidate 1 --bind --confirm-parent-write \
  --approve-analysis-only --confirm-source-unverified
```

This command was NOT executed during implementation. It rechecks source hashes,
40-day/future-caption eligibility, exact shortcode across all PostgreSQL accounts,
caption/publication revision, existing child extraction, import acknowledgment and
parent request journal. Known canonical/legacy imports in the local snapshot are
negative guards; absence is NOT an authoritative MySQL no-import verification.
Unavailable/conflicting state fails closed. Raw captions/image URLs are never
printed. No compound-ID semantics or accepted collaboration are inferred.

Creation uses the existing scraper mapping and unique natural key with
ON CONFLICT DO NOTHING, under a canonical-shortcode advisory lock. It never updates
an existing parent. The actual database-generated parent ID is then bound into a
new immutable, private, analysis-only batch manifest. Original ordered images and
caption remain in the existing input contract. `comments_json.analysis_review`
retains source object/revision hashes, discovery profiles and uncertainty flags.
This is a quarantine marker, not a source verification attestation.

Normal serial/multithreaded analyzers, discovery candidate selection and bounded
normal parent processing exclude these quarantined parents. Normal scraper
refreshes preserve the marker. There is intentionally no automatic release or
production-import path. Do not manually remove the marker merely because Gemini
finds an event; source/identity/collaboration/integrity review remains separate.

The binding JSON returns `next_command` (read-only analysis preflight) and
`analysis_command` (separately approved one-request execution). Copy those actual
commands after binding; do not invent a parent ID or run the entire discovery
pipeline. The existing analysis-only executor keeps its shared journal/locks,
one-HTTP cap, daily budget checks, original-image load requirement, private local
result recovery, and no automatic paid retry. A successful local result is NOT a
committed production extraction and does not authorize a cover/import.

All three may be separately approved for local content analysis AFTER binding
and budget availability. All three remain blocked from production import.

## Automatic binding through the existing multi-account collector

No per-candidate parent ID or binding command is required. Review all configured
accounts from existing staging, without collection, writes, or paid requests:

```bash
./venv/bin/python collect_instagram_posts.py --all-profiles --ingest-staged \
  --preflight --max-parents 5 --gemini-max-http-requests 1
```

Separately approved bounded real batch (one parent and at most one Gemini HTTP
request TOTAL; may create its quarantined parent):

```bash
./venv/bin/python collect_instagram_posts.py --all-profiles --ingest-staged \
  --run --max-parents 1 --gemini-max-http-requests 1 \
  --approve-parent-writes --approve-analysis-only \
  --confirm-source-unverified --confirm-paid-requests
```

Selection is automatic from current staged content, not pinned to a manual ID.
These flags approve local content analysis of untrusted source material only.
They DO NOT attest Instagram authenticity or authorize import/cover generation.
Use `--collect-and-ingest` instead of `--ingest-staged` for a future separately
authorized collection-first invocation; it reuses the existing shared collector,
then the same binding/analysis code. Collection failure stops the handoff.
Neither mode enables scheduling, Recovery or Dashboard.

The batch checks live PostgreSQL parents/acknowledgments and shared request
journals before selection and again during binding/dispatch. Failed or completed
parents are excluded before the bounded selection; parent 1645 remains blocked.
Known imported/legacy markers in the existing local index remain negative guards;
absence is not a fresh authoritative MySQL no-import verification. Exact source
hashes, account binding, caption dates, first original image, and input revisions
remain enforced by the existing helpers. Unresolved compound IDs/source and
collaboration hints stay attached, never upgraded to trusted evidence.

The JSON separates would-create/would-reuse counts, actual created/reused counts,
analysis readiness after binding, current readiness/budget, exclusion reasons,
known HTTP dispatch count and any uncertain dispatch accounting. A global batch
slot is consumed before each executor invocation, including an unexpected failure.
Each parent still has a one-HTTP cap and durable no-retry journal; the existing
20 Gemini/3 cover daily configured limits and initialized-day ledger pins remain
unchanged. Provider 429 stops further analysis in the batch even if local budget
remains. Cap zero permits explicitly approved binding only, with no paid dispatch.

Quarantined/local-analysis parents never advance automatically into covers or
Event Master. Their parent IDs, source hashes, local events[] results and unchanged
canonical marker identity are the handoff to the existing parent pipeline AFTER
independent source/integrity/identity verification, future-event validation,
production approval and a reviewed quarantine release/persistence step. No release
or verification producer is fabricated here; no currently staged candidate has
production approval. Existing cover and importer implementations remain unchanged.
