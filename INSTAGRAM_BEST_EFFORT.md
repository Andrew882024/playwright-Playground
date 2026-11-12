# Explicit best-effort processing

This mode supersedes the earlier all-or-nothing sixthcoboard capture experiment.
It does not claim full timeline coverage, authenticate unknown sources, generate
covers, or authorize Event Master imports.

```
./venv/bin/python -m app.instagram_scraper.sync_ins --profile sixthcoboard --headless --best-effort
```

The discovery CLI also accepts `--best-effort`. Valid data advances to screening;
partial capture remains visible as `partial` / integrated `partial_failure`.
Strict capture remains available by omitting the flag. The account list is not
modified. Existing staging/review commands keep their own stricter policies.

In best-effort mode, failed GraphQL responses remain counted and are skipped.
Known profile connections can retain individually validated edges; unknown
connections are not promoted to profile data. Per-post projection failures leave
previously parsed data intact. Bad post metadata or absent image references are
reported with a shortcode when available. Write transactions commit independently
per distinct shortcode, preserving already committed posts if a later row fails.
Connection failures remain global. Login/challenge, closed browser, HTTP 401/403
and recognized authentication error codes still block the capture. A run without
any valid timeline response cannot advance. No historical records are deleted.

For this run only, explicit IDs from the just-completed scrape were supplied to:

```
./venv/bin/python run_instagram_best_effort_analysis.py --profile sixthcoboard \
  --post-id 2127 --post-id 2128 --post-id 2130 --post-id 2134 --post-id 2135 --run
```

Without `--run`, this command only displays the explicit selection. It accepts at
most five IDs, rechecks account/unanalysed state under existing parent locks,
requires the first original image, and reuses the existing Gemini model/prompt,
shared daily ledger, durable request journal and atomic extraction persistence.
Each post has at most ONE SDK generation dispatch; SDK retries, model fallback
and extra JSON-repair dispatches cannot spend another request. Ordinary post
failures continue to the next ID. Database unavailability and provider
401/403/429/budget failures block the remaining batch. Non-events persist
`is_event=false` with no children; events persist existing parent and child fields.
Failed extractions remain unanalysed. Paid/uncertain attempts retain their journal
for explicit future retry review; no automatic paid retry is enabled.

The runner never scrapes, backfills images, generates covers or imports activities.
No schema change is required. Diagnostics contain IDs, fixed reasons and status
codes, not credentials, raw provider messages or signed URLs.

## Observed run outcome (2026-11-11 LA)

* Scrape: partial; 20 GraphQL responses, five valid timeline responses, one failed
  response skipped and fourteen unrelated responses ignored. Sixty unique captured
  posts yielded eleven within the existing forty-day window. Eleven parents were
  inserted (IDs 2127–2137), zero updated, zero per-post write failures, no duplicate
  profile/shortcode keys. The other 49 captured posts were outside the window.
* Analysis: attempted IDs 2127, 2128, 2130, 2134, 2135. Four requests returned HTTP
  503 and the fifth HTTP 504. Five SDK generation dispatches total, zero successful
  extractions, zero analysis skips. The shared ledger independently recorded five
  Gemini requests and zero cover requests. Each failure continued to the next ID.
* All eleven parents remain unanalysed, with no child events. The six unselected
  posts were not sent to Gemini. Caption event announcements are candidates only;
  this run did not confirm any event through Gemini. No retries were issued.
* Final relevant suite: 155 tests passed. A broader staging-ingestion suite exposed
  17 errors from its existing configured_accounts maximum of sixteen: the current
  user-managed TARGET_URLS already contains seventeen accounts including
  sixthcoboard. Neither the account list nor that unrelated review limit was changed.
