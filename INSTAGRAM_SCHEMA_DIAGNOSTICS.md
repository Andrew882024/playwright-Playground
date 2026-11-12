# Opt-in Instagram schema diagnostic

Normal discovery does not enable this probe or retain unknown field names. The
separate CLI defaults to inspection-only. `--capture` always uses the dedicated
browser-only `schema_capture.capture()` path for exactly one configured profile.
It never calls discovery, the normal scraper main/profile-upsert routine, Gemini,
OpenAI, original-image backfill, covers, import, Recovery or Dashboard. There is no
fallback to a write-capable runner. Paid request counts are zero by construction.
A failed scrape still fails. No parser or acceptance rule is changed.

The capture uses the existing cookie loader, response parser, authentication checks,
scroll limits and health checks. Its ephemeral Firefox context does not modify the
normal persistent browser profile, take screenshots, save storage state or record
traces. Known-post overlap checks and PostgreSQL advisory locks are not used: this
path neither imports the persistence adapters nor queries/updates PostgreSQL.
Database imports in normal `sync_ins` are lazy; normal discovery continues to use
its existing PostgreSQL locks and upserts. A fresh-process offline test forbids all
DB and downstream imports while exercising capture success, empty, failed and
interrupted outcomes. Browser-only capture does not touch execution logs, paid
ledgers or request journals. It emits only safe JSON to the terminal.

The probe inspects at most 128 error items per response and 32 path segments. It
only considers the first unmatched field below a recognized timeline media node
(or the existing fixed media wrapper positions). It requires an actual POST query
document and an unaliased selection along the complete prefix. Request variables,
cookies and URL query strings are never used for schema evidence. Only the
content-type header is mapped to a fixed MIME category; other header values
are never retained or emitted.
A persisted-query `doc_id` alone is insufficient: it reports `query_unavailable`.
Fragments, directives, multiple operations and other unsupported query syntax are
rejected; this deliberately restricted parser is not a GraphQL implementation.

Candidate labels must be 3–48 lowercase letters/underscore-separated words, with
no digits, no long unbroken/hex-like identifiers, and no sensitive components such
as token/password/username/session/url.
These checks reduce risk; they do not prove a field is a valid provider schema
field, or that an error on it is harmless. Up to eight candidate labels and their
fixed wrapper placement/count are kept in memory only. Normal JSON and execution
logs receive fixed categories/counts, never candidate labels. No raw payload,
message, query, URL, user information, or actual path/index is persisted by the
probe. The normal discovery execution log remains unchanged.

For one FUTURE authorized capture, in an interactive terminal:

```sh
cd /home/zoewu/cssa/instagram/playwright-Playground
./venv/bin/python diagnose_instagram_graphql_schema.py \
  --profile dvcatucsd --headless --capture --review \
  --fixture /tmp/dvcatucsd-reviewed-schema.json
```

Do not run this command until a live scrape is authorized. It runs once even on
failure. PostgreSQL is not used even if scrape health passes. No raw responses
are saved, and no discovery execution log is created. The only optional application
artifact is the individually reviewed schema fixture described below.

After capture, validated candidates are displayed on the interactive stderr
terminal only. Avoid terminal session recording/shell output redirection if names
have not yet been reviewed. Type the exact candidate label to approve retaining
that label, or press Enter to reject it. No approval is inferred. The fixture is
created exclusively with mode 0600, without overwriting another file. Only approved
field labels, fixed placement/evidence enums and counts are written. It contains
no actual Instagram paths, root/query names, IDs or field values. Review the fixture
before sharing it or using it to add classifier vocabulary. Never infer optionality
or change health acceptance automatically from this fixture.

Omit `--review --fixture ...` for fixed-count-only capture: candidate labels are
then discarded at process exit. A persisted-query-only capture may yield no names;
that is an evidence limitation, not a reason to guess fields or relax validation.

## Query transport diagnostics

`query_unavailable` counts error objects, not requests. `query_transport.responses`
counts response inspections (once per response when a candidate reaches query
evidence checking). One response with many errors therefore adds one transport
observation but can add many query-unavailable errors.

The separate probe reports fixed transport/encoding/method/content-type counts
and presence-indicator counts. It recognizes inline JSON/form `query`, JSON/form
`doc_id`, `query_hash`/`queryHash`, and Apollo-style
`extensions.persistedQuery.sha256Hash` (including JSON-encoded form extensions).
Operation-name and friendly-operation-name fields are reported by presence only.
IDs, hashes, operation names, queries, variables, bodies and header values never
appear in these diagnostics or remain in probe state. Presence is structural
evidence, not proof of a valid provider request.

Missing request access, body access/decode failures, absent or oversized bodies,
malformed JSON/form data, batches, non-object JSON, duplicate/invalid query fields,
and unsupported encodings have distinct fixed categories. Raw application/graphql
and multipart bodies are classified as unsupported; GET URL query strings are
intentionally not inspected. Inline documents still pass the existing restricted
selection parser and alias/privacy checks. No persisted query is fetched,
reconstructed or replayed, and no acceptance rule is changed.

For one future authorized fixed-count-only check, omit interactive review and
fixture output:

```sh
./venv/bin/python diagnose_instagram_graphql_schema.py \
  --profile dvcatucsd --headless --capture
```

This remains browser-only, with no database or downstream calls. Existing logs
without transport counts cannot establish which request shape caused an earlier
query-unavailable result.

## Partial capture: visible fields only

For one FUTURE separately authorized database-free test:

```sh
./venv/bin/python diagnose_instagram_graphql_schema.py \
  --profile dvcatucsd --headless --capture --partial-capture
```

`--partial-capture` requires `--capture`; it cannot be combined with `--review` or
`--fixture`. It does not enable the query-document probe or inspect request query
bodies. No field names or candidate posts are released. There is no discovery,
database, paid generation, image download/upload, import, recovery or dashboard.

The existing decoder and fixed flat/node.media positions are observed before
production rejection. The original handler still rejects errored responses and
null/malformed slots unchanged. `captured_posts` remains the production-accepted
count; partial candidates have separate counters and are never added to its body
or shortcode outputs. The diagnostic's overall status is ALWAYS
`partial_unverified`, including empty, failed and interrupted runs. CLI exit code
remains nonzero. `capture_outcome` separately reports completed/failed/interrupted;
`scrape_health` retains the original fail-closed status and reason.

Visible-field validation requires:

* A 1–32 character ASCII Instagram code (letters, digits, underscore, hyphen).
  IDs are never converted into invented shortcodes.
* An explicit valid `user.username` matching the selected configured account,
  ignoring case. Visiting the account's page alone supplies no identity proof.
* Positive integer Unix publication seconds, representable in UTC and at most
  five minutes beyond the diagnostic start. `device_timestamp` is used only when
  `taken_at` is absent. No event-date inference or 40-day processing is performed.
* A present `caption` object with string `text`; explicit empty text is allowed,
  while null/missing/malformed caption data is incomplete.
* At least one HTTPS image-field reference on an exact Instagram/CDN host or its
  subdomain, without credentials, unusual ports, fragments or whitespace. Video
  URLs and avatars are not images. No HTTP request checks availability or MIME.
* Known photo/video type, or a recognized carousel. A carousel requires an
  explicit positive matching child count, distinct child identities and an image
  reference on every child; forward pagination or missing/malformed children is
  incomplete. Unknown counts are not assumed complete. Production media-resolver
  validation is retained as an additional structural check.

This is visible-field validation, NOT independent verification of provider data,
account ownership, image availability, carousel exhaustiveness, or timeline
coverage. Unknown GraphQL errors remain unresolved even when visible fields pass.
Do not use these counts to enable production writes/imports.

The one JSON result contains the normal capture-only safety counters plus:

```json
{
  "status": "partial_unverified",
  "capture_outcome": "failed",
  "partial_capture": {
    "status": "partial_unverified",
    "timeline_completeness": "unverified",
    "production_eligible": false,
    "image_availability": "not_checked",
    "source_verification": "payload_username_match_only",
    "counts": {
      "responses_inspected": 0,
      "candidate_media_nodes": 0,
      "unique_post_identities": 0,
      "fully_valid_candidates": 0,
      "unique_fully_valid_candidates": 0,
      "incomplete_candidates": 0,
      "duplicate_candidates": 0,
      "null_media_slots": 0,
      "malformed_media_slots": 0,
      "unsupported_media_slots": 0,
      "responses_with_graphql_errors": 0,
      "responses_rejected_by_production": 0,
      "inspection_truncated_responses": 0,
      "diagnostic_errors": 0
    },
    "incomplete_reasons": {
      "invalid_identity": 0,
      "source_account_missing": 0,
      "source_account_mismatch": 0,
      "invalid_timestamp": 0,
      "missing_caption": 0,
      "invalid_caption": 0,
      "missing_image": 0,
      "media_type_unverified": 0,
      "carousel_count_unverified": 0,
      "carousel_incomplete": 0,
      "invalid_media_structure": 0,
      "validation_exception": 0,
      "identity_tracking_limit": 0
    }
  }
}
```

The zeros above illustrate the schema, not a live result. Counters count
observations unless explicitly named unique; duplicate observations are never
merged to manufacture a complete candidate. Reasons can overlap per candidate.
Null/malformed/unsupported slots are counted separately from candidate objects.
Inspection is bounded to 64 top-level connections and 128 edges per response,
100 children per carousel, and 5,000 in-memory keyed identity digests. These
random-key digests are never emitted or persisted. Raw captions, identities,
account names, image URLs, query documents and response bodies are never saved
by partial capture. Bodies are cleared during cleanup. Truncation and diagnostic
errors invalidate any completeness interpretation, as does the always-unverified
status itself.

### Partial candidate provenance

Partial extraction is not bound to a verified request for the configured profile.
The page listener sees all Instagram GraphQL responses during navigation/scrolling.
The diagnostic considers exact known timeline roots, timeline-like root names and
other top-level media-shaped edge collections. A matching `media.user.username`
only passes the existing visible-field check; it does not prove that the collection
belongs to the requested profile or that an observation represents a unique post.

`partial_capture.candidate_provenance` aggregates only fixed enums/booleans/counts:

* `connection_kind`: known_timeline_root, timeline_name_hint, generic_media_edges.
* `wrapper_kind`: flat_node or node_media.
* `response_has_errors`: boolean.
* `author_relation` / `owner_relation`: matches_requested, different_account,
  missing_or_invalid. Only the existing `media.user.username` controls validation;
  alternate `owner.username` is a diagnostic comparison, never an automatic fix.
* `owner_author_relation`: agree, disagree, unavailable.
* `coauthor_shape`: absent, null, empty_list, user_object_list, malformed, truncated.
* `requested_account_in_coauthor_hint` and `coauthor_inspection_truncated`: booleans.
* `validation_outcome`: visible_fields_valid or incomplete.
* `count`: observation count, not unique post count.

Only the fixed `coauthor_producers` position is inspected (at most 32 direct user
objects with a valid username). This is a shape/membership hint, NOT proof that
Instagram has accepted a collaboration or that the media belongs on a profile.
The existing repository's schema-probe examples are synthetic; they do not verify
live collaboration semantics. Pending invitations are not used. Missing metadata
is not evidence of no collaboration, and truncated metadata cannot establish a
negative match. No username, identifier, raw connection name or metadata value is
included in these patterns. Partial mode now transiently inspects bounded request
variables as described below; it does not inspect query documents or save bodies.

The result always includes `request_profile_binding: not_verified` and
`collaboration_semantics: unverified_hints_only`. These diagnostics never override
a source mismatch, change production rejection, or make a candidate eligible for
writes/imports. Previously stored aggregate counts cannot retrospectively establish
which mismatch category occurred; a separately authorized database-free capture
would be required to populate these new counts.

### Request-to-profile binding (partial capture only)

The isolated handler inspects the corresponding Playwright `response.request`
only for Instagram GraphQL responses. This is passive metadata access, not a new
network request. It reads at most 131,072 POST-data characters, with at most 128
form fields and 65,536 JSON-encoded-variable characters. JSON and form bodies are supported;
duplicate keys, malformed variables, batches and unsupported encodings fail closed.
No page URL, referrer, payload author or coauthor is used to verify request binding.

`partial_capture.request_binding_counts` counts responses in four fixed categories:
`verified_match`, `verified_mismatch`, `ambiguous`, `unavailable`.
`request_binding_reason_counts` explains them with fixed reason enums. These
counts include responses without candidate posts; candidate patterns count media
observations, so their totals can differ.

There is **no reviewed Instagram operation/selector contract** in this repository.
Accordingly neither verified category is currently emitted. A doc_id, operation
name, inline query or a variable named `username` alone cannot prove that a private
operation requests that profile's timeline. Numeric IDs cannot be compared to a
username without an independently verified mapping. Missing/inaccessible request
metadata is `unavailable`; readable but unreviewed or malformed target evidence is
`ambiguous`. This deliberately cannot retrospectively verify the earlier 63 media
observations.

Only direct variable positions `username`, `target_username`, `profile_username`
are compared transiently. The fixed relation is `matches_requested`,
`different_account`, `conflicting`, or `missing_or_invalid`. Direct `id`, `user_id`,
`target_user_id`, `profile_id` yield only a `numeric_target_hint` boolean. These
positions are unverified hints, not an asserted Instagram API contract. There is
no recursive search or retention of unknown keys/values.

Each `candidate_provenance` pattern additionally contains `request_binding`,
`request_username_hint_relation`, and `numeric_target_hint`. Together with existing
author relation, coauthor membership hint and validation outcome, these form an
aggregate cross-tabulation. The callback retains only this safe projection and
restores its previous context after each response, including interruption. No raw
request, variables, usernames, IDs, doc_ids, hashes or operation names leave it.

Before enabling `verified_match`, a separately reviewed contract must establish
which operation and selector unambiguously target the returned timeline, including
selector conflicts, aliases, and any numeric-ID-to-profile mapping. This change
adds diagnostics only: production acceptance, null handling, mismatched-author
rejection and `partial_unverified` remain unchanged.

The next validation requires separate live authorization. One database-free capture:

```bash
./venv/bin/python diagnose_instagram_graphql_schema.py \
  --profile dvcatucsd --headless --capture --partial-capture
```

It will report only aggregates, not unknown selector names or values, and cannot
by itself establish the missing operation contract. Do not enable ingestion from
these results.
