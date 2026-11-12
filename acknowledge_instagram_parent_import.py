"""Verify an existing parent import; default dry-run, --acknowledge writes PG metadata only."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--post-id',type=int,required=True)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run',action='store_true')
    modes.add_argument('--acknowledge',action='store_true')
    args=parser.parse_args(argv)
    if args.post_id<=0:parser.error('post-id must be positive')
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    from app.pipeline_recovery.acknowledge import acknowledge,AcknowledgmentError
    try:result=acknowledge(args.post_id,write=args.acknowledge)
    except AcknowledgmentError as exc:
        # Only fixed authored reason codes; never echo arbitrary exception text.
        allowed={'verification_unavailable','no_activities','parent_marker_not_unique','legacy_conflict',
            'unexpected_event_owner','fields_differ','verification_changed','parent_ineligible',
            'source_revision_changed','active_lease'}
        result=dict(status='rejected',reason=str(exc) if str(exc) in allowed else 'verification_unavailable')
    except Exception:result=dict(status='rejected',reason='verification_unavailable')
    print(json.dumps(result,sort_keys=True))
    return 0 if result['status'] in ('dry-run','succeeded') else 1

if __name__=='__main__':raise SystemExit(main())
