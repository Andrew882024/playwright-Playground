# Standalone parent Gemini model override

`run_instagram_parent_pipeline.py --parent-id <id> --gemini-model <gemini-model-id>`
selects exactly one model for that one parent invocation. Preflight remains the
default and dispatches no requests. The override is passed as a local analyzer
argument; it never changes GEMINI_MODEL, GEMINI_MODEL_FALLBACK, the configured
chain function, environment files, scheduler, bulk runner, or other parent runs.
An override supplies a singleton chain, with no automatic fallback to another
model. The analyzer rejects overrides without an explicit source post ID.

For an operator-reviewed, manually authorized retry of Parent 460:

```bash
python run_instagram_parent_pipeline.py \
  --parent-id 460 \
  --gemini-model gemini-3-flash-preview \
  --gemini-max-http-requests 1 \
  --authorize-gemini-retry \
  --confirm-gemini-retry \
  --run
```

This is the full standalone stored-parent pipeline: successful extraction can
continue to its existing original-image, cover and Event Master stages. It never
runs the scraper. The command above was NOT executed during implementation and
does not establish availability/quota for the requested Gemini model.

Changing the model does not reset request safety or authorize a retry. Both retry
flags remain mandatory after review. A confirmed retry authorizes one new request;
old request entries are preserved. A failed/uncertain new request blocks another
automatic attempt, even if the configured HTTP budget is greater than one. SDK
internal retries remain disabled for durable parent attempts. A zero request
budget dispatches nothing and creates no request intent.

Every new durable request record includes a validated `model` when available,
written with the intent BEFORE SDK dispatch. Legacy records remain readable and
are never backfilled or reset just to add a model. Safe diagnostics expose
selected_model and, for override runs, gemini_model_override and
 gemini_automatic_model_fallback=false. They retain no credentials, prompts,
source URLs, response bodies or raw exception strings. Invalid model identifiers
are rejected before execution; only Gemini model IDs are accepted.

The existing Parent 460 journal was not modified. Offline tests use temporary
journals and mocked Gemini, persistence, cover and import interfaces only.
