"""Sync explicit parent posts only. Default dry-run makes no importer/DB calls."""
import argparse
import json
import sys
from pathlib import Path

from sync_instagram_children_to_event_master import positive_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--post-id', type=positive_id, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true')
    modes.add_argument('--sync', action='store_true', help='Authorize one real parent upsert')
    parser.add_argument('--adopt-event-id', type=positive_id,
                        help='Explicit existing Event Master child event to adopt; never automatic')
    args = parser.parse_args(argv)
    if not args.sync:
        print(json.dumps(dict(status='dry-run', post_id=args.post_id, adopt_event_id=args.adopt_event_id)))
        return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / '.env')
        from app.event_master_sync.sync_service import sync_posts
        result = sync_posts([args.post_id], adopt_event_id=args.adopt_event_id)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'completed' else 1
    except Exception:
        print('Parent Event Master sync failed. Details withheld; check configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
