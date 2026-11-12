"""Explicit analysis-only staged pilot; preflight by default, no production writes."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--parent-id', type=int, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--preflight', action='store_true')
    parser.add_argument('--confirm-one-paid-request', action='store_true')
    args = parser.parse_args(argv)
    if args.run != args.confirm_one_paid_request:
        parser.error('Execution requires both --run and --confirm-one-paid-request')
    from dotenv import load_dotenv
    root = Path(__file__).resolve().parent
    load_dotenv(root / '.env')
    from app.instagram_scraper.analysis_execution import execute
    result = execute(root / '.instagram-post-staging', args.batch_id, args.parent_id, run=args.run)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in ('preflight', 'analyzed_locally', 'reused_local_result') else 1


if __name__ == '__main__':
    raise SystemExit(main())
