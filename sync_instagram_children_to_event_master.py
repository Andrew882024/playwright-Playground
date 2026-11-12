"""Sync explicitly selected child IDs only; default is a no-import dry run."""
import argparse
import json
import sys
from pathlib import Path


def positive_id(value: str) -> int:
    try:
        child_id = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Child ID must be a positive integer.') from None
    if child_id <= 0:
        raise argparse.ArgumentTypeError('Child ID must be a positive integer.')
    return child_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child-id', type=positive_id, action='append', required=True,
                        help='Explicit instagram_events.id; repeat for additional children')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true', help='Print child IDs only; no importer/database calls (default)')
    modes.add_argument('--sync', action='store_true', help='Explicitly authorize importer execution for these IDs only')
    args = parser.parse_args(argv)
    ids = sorted(set(args.child_id))
    if not args.sync:
        print(json.dumps(dict(status='dry-run', child_ids=ids), sort_keys=True))
        return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / '.env')
        from app.event_master_sync.sync_service import sync_children
        # Explicit CLI authorization does not enable the automatic pipeline flag.
        result = sync_children(ids)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'completed' else 1
    except Exception:
        print('Event Master sync failed; check configuration. No raw subprocess output is logged.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
