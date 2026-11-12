# Parent presentation and covers — Phase 1

A post has one parent presentation and one parent cover. Its `instagram_events`
rows remain the structured activities. This phase does not change Event Master
imports, migrate existing Event Master rows, or change extraction.

## Explicit migration

Run manually from this repository with the intended Playground DB configuration:

```bash
python add_instagram_post_presentation_columns.py
```

This idempotently adds nullable `collection_title TEXT`, `generated_image_url TEXT`,
and `generated_image_input_hash VARCHAR(64)` to `instagram_posts`. It performs no
backfill and does not touch child columns. Model registration alone does not add
columns to an existing table; run this migration before deploying parent consumers.
No migration has been run as part of implementation.

## Presentation

`build_post_presentation(parent, children)` is pure and accepts ORM objects or dicts.
It retains all supplied children and sorts by known UTC start, title, then child ID.
A single child supplies the title. Multiple children use a nonempty
`collection_title`, otherwise known provider(s) and known Los Angeles dates.
Missing dates, locations, and providers are not inferred. The earliest start is the
earliest known instant, with a separate missing-start indicator. Different known
locations yield `Multiple Locations`; incomplete single-location data remains
unknown. Multiple activities always have a null aggregate duration and individually
labeled schedules. All description text is HTML-escaped. The canonical fingerprint
includes all activity facts, but excludes child generated-cover state.

## Parent cover command (not run during implementation)

```bash
python generate_instagram_post_cover.py --parent-id 6 --dry-run
python generate_instagram_post_cover.py --parent-id 6 --generate
python generate_instagram_post_cover.py --parent-id 6 --resume-only
```

Dry run loads presentation only and creates no artifacts. Generation requires the
explicit paid-call authorization flag; resume-only cannot authorize generation.
The shared state machine retains intent-before-paid-call manifests, 180-second HTTP
timeout without automatic paid retries, PNG validation, uncertain-request blocking,
per-target filesystem locks, and saved upload/publication recovery. The new prompt
`generated-parent-cover-v1-aggregate` includes the aggregate title and ALL activities
as JSON context and requests exactly one image.

Parent artifacts are isolated:

- Cache/lock: `EVENT_COVER_CACHE_DIR/parents/<parent-id>/...`
- Private S3: `instagram_post_covers/<parent-id>/<version>/<input-hash>/<request-id>.png`
- Publication: only `instagram_posts.generated_image_url/generated_image_input_hash`

Publication locks the parent before its children and rejects changed presentation,
source, or competing parent cover state. Zero-child posts are not generated.
Existing published parent covers are skipped; changing a title is not authorization
to overwrite/regenerate an existing cover automatically.

The optional pipeline cover stage selects distinct parent rows only from this run's
successfully committed parent IDs, with at least one child. The existing
`INSTAGRAM_PIPELINE_COVER_MAX_ATTEMPTS` cap now counts parent attempts (including
failures/recovery), never child activities. Stage results use `parents`/`parent_id`.
The existing enable flag stays opt-in and its default stays disabled.

Legacy child generation entry point, child generated-image columns, child-ID cache
folders and `instagram_event_covers/*` S3 keys remain supported and untouched.
In particular child 7's cover is never read as a parent cover, promoted, deleted,
or rewritten by the parent path. The existing child-based Event Master sync is
unchanged in this phase; do not enable it expecting parent-aggregate imports.
