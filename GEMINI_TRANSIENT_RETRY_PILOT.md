# Bounded Gemini retry pilot

Scope: only existing sixthcoboard parents 2127 and 2128, within the approved
2127–2137 range. No scraping, schema changes, covers, image uploads or imports.

## Diagnosis

The existing environment and prior durable journals both selected
`gemini-3.8-flash`. The SDK resolved the base URL to
`generativelanguage.googleapis.com`. No HTTP_PROXY, HTTPS_PROXY, ALL_PROXY or
Gemini/Vertex base-URL override was present. These observations establish local
routing configuration, not proof against a transparent network intermediary.
The previous diagnostics recorded SDK ServerError HTTP responses (503/504), not
client-side timeout exceptions; raw response bodies were not retained.

Legacy generation has three transient-server attempts with 2/4-second exponential
backoff, a separate 429 retry loop and a configurable fallback chain. The bounded
best-effort path deliberately used a strict ONE-dispatch budget, SDK attempts=1
and a one-model chain, so those legacy retries/fallbacks could not run.

Existing image loading uses a 30-second download timeout and rejects individual
images over 18 MiB. Generation uses a 120000-ms SDK timeout. The new diagnostic
projection records image count/total/largest byte size and prompt character count
without source content. No token count is inferred from character counts.

## Explicit retry mode

`run_instagram_best_effort_analysis.py --retry-transient` permits only up to TWO
existing IDs within 2127–2137. Each post makes at most THREE generation dispatches
in this invocation, with waits of 2 and 4 seconds. Only 429/500/502/503/504 SDK HTTP
failures in the generation phase permit a new request. No retries for 400,
authentication, unknown transport outcomes, image failures, malformed output or
persistence failures. No model switch. Each attempt reserves the existing shared
daily ledger and appends to the original durable journal; old history is retained.
Prior uncertain/received-but-uncommitted outcomes remain blocked. Ordinary
exhausted posts continue to the second ID. Authentication/database/budget failures
still stop the batch. Failed extraction leaves PostgreSQL unanalysed.

Successful results reuse existing Gemini event recognition and atomic
`persist_analysis`, including parent fields, is_event and child events. This
analysis runner does not call the parent pipeline's backfill/cover/import stages.

Provider diagnostics expose only fixed status enums and boolean overload/deadline
hints, official-host/JSON/HTTP-response flags and HTTP codes. API keys, headers,
raw error bodies and signed URLs are not logged.

Tests: 91 passed across retry, analysis, diagnostics, request budgets, journal
safety, extraction and daily budgets. Tests cover 2/4-second backoff, three-total
cap, first-post exhaustion followed by second-post success, allowed status codes,
nonretryable errors and uncertain prior outcomes.

## Observed retry outcome

Only 2127 and 2128 were processed. Both remained unsuccessful after their bounded
attempts. Current invocation: SIX dispatches (two first attempts plus four retries),
four HTTP 503 and two HTTP 504. Prior invocation's five requests are separate;
the shared daily ledger now shows eleven total and zero cover requests.

* 2127 / Diwali: 504, 504, 503; one original image, 241816 bytes, 4891 prompt chars.
* 2128 / movie night: 503, 503, 503; one original image, 233374 bytes, 5103 prompt chars.

Every failure had an SDK HTTP response, JSON content type and a request hostname
matching the official API. The 503 error objects had `UNAVAILABLE` status and an
overload/high-demand hint; 504 had `DEADLINE_EXCEEDED` and a deadline hint. This
supports provider-side overload/deadline as the likely cause, rather than an
observed configured proxy or local timeout exception. It cannot prove the absence
of transparent network infrastructure. Payload metrics do not support an oversized
image/input diagnosis; no text-only fallback, image resizing, model switch or
larger timeout experiment was performed.

PostgreSQL verification: 2127 and 2128 remain ai_analyzed=false/is_event=NULL,
with no child events. All eleven source parents remain present and unanalysed.
No future event has been confirmed for Event Master by this run. No further
requests were sent after the two-post retry pilot.
