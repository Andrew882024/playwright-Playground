# Parent Event Master sync — Phase 2

Automatic sync now passes distinct committed parent IDs directly to the backend
importer. It never expands them into child IDs. A parent with five activities
therefore produces at most one parent cover attempt and one importer attempt in
a pipeline invocation. The existing opt-in flag
INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER remains disabled by default. Cover failures
are reported separately and do not block sync; original-image fallback remains.

Explicit CLI (default dry-run prints selection only; no DB or importer calls):

```bash
python sync_instagram_posts_to_event_master.py --post-id 6
python sync_instagram_posts_to_event_master.py --post-id 6 --sync
```

`--adopt-event-id <mysql-id>` is available only for an explicit reviewed adoption;
the automatic stage never supplies it. Backend checks must still pass. Conflicts
and adoption-required outcomes fail safely without deletion or merging. The old
sync_instagram_children_to_event_master.py remains for legacy recovery; it is no
longer used by the automatic pipeline. The backend legacy child path now refuses
posts already represented by a parent aggregate.

Each subprocess runs in Event Master's directory/interpreter, with a credential
allowlist and a nonsecret INSTAGRAM_PLAYGROUND_DIR pointing to this checkout's
pure Phase 1 presentation implementation. Playground credentials/PYTHONPATH are
not forwarded. Importer output is suppressed; only safe status/reason codes are
reported. Timeout means commit status is unknown; there is no automatic retry.

Apply the Phase 1 parent-column migration explicitly before enabling this path.
No migration, real sync, database update, pipeline execution, or external call was
performed during implementation. Existing child-based Event Master events and
child 7's cover remain untouched until an explicitly authorized reconciliation.
