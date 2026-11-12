"""Read-only parent lookup by default. Binding explicitly creates analysis-only rows."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--candidate', type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--bind', action='store_true')
    parser.add_argument('--confirm-parent-write', action='store_true')
    parser.add_argument('--approve-analysis-only', action='store_true')
    parser.add_argument('--confirm-source-unverified', action='store_true')
    args = parser.parse_args(argv)
    if len({args.bind, args.confirm_parent_write, args.approve_analysis_only, args.confirm_source_unverified}) != 1:
        parser.error('Binding requires --bind --confirm-parent-write --approve-analysis-only --confirm-source-unverified')
    if args.bind and args.candidate is None:parser.error('Bind exactly one explicit --candidate')
    from dotenv import load_dotenv
    root = Path(__file__).resolve().parent
    load_dotenv(root/'.env')
    from app.instagram_scraper.parent_binding import bind_candidate, Blocked
    staging = root/'.instagram-post-staging'
    try:
        # Validate manifest through the same loader, never accept arbitrary paths.
        if args.candidate is not None:
            indexes = [args.candidate]
        else:
            import re
            if not re.fullmatch('[a-f0-9]{64}', args.batch_id):raise Blocked('invalid_batch')
            manifest = json.loads((staging/'analysis-batches'/(args.batch_id+'.json')).read_text())
            indexes = [r['index'] for r in manifest['candidates']]
            if not 1 <= len(indexes) <= 5:raise Blocked('invalid_batch_size')
        records = []
        for index in indexes:
            try:records.append(bind_candidate(staging,args.batch_id,index,write=args.bind))
            except (Exception,SystemExit) as exc:
                records.append(dict(candidate=index,status='blocked',reason=str(exc) if isinstance(exc,Blocked) else 'parent_binding_unavailable'))
        result = dict(status='blocked' if any(r['status']=='blocked' for r in records) else 'bound' if args.bind else 'dry_run',
            candidates=records,paid_requests=0,ready_for_production_import=0)
    except (Exception,SystemExit) as exc:
        result = dict(status='failed',reason=str(exc) if isinstance(exc,Blocked) else 'parent_binding_unavailable')
    print(json.dumps(result,sort_keys=True))
    return int(result['status'] in ('failed','blocked'))


if __name__ == '__main__':raise SystemExit(main())
