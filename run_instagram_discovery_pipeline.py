"""Bounded discovery-to-parent pipeline. Default preflight never scrapes or writes."""
import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--best-effort',action='store_true',help='Allow valid posts from partial scrapes to proceed')
    parser.add_argument('--profile',help='One configured account; default all configured accounts')
    parser.add_argument('--headless',action='store_true',help='Run scraper Firefox without a graphical window')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight',action='store_true')
    modes.add_argument('--run',action='store_true')
    parser.add_argument('--gemini-max-attempts',type=int)
    parser.add_argument('--cover-max-attempts',type=int)
    parser.add_argument('--max-parents',type=int,default=10)
    parser.add_argument('--override-review-parent-id',type=int,action='append',default=[])
    parser.add_argument('--confirm-review-override',action='store_true')
    args=parser.parse_args(argv)
    if bool(args.override_review_parent_id)!=args.confirm_review_override or any(i<=0 for i in args.override_review_parent_id):
        parser.error('Override requires explicit positive parent IDs and --confirm-review-override together')
    configuration_error=None
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent/'.env')
    except Exception as exc:configuration_error=exc
    def execute_run():
        if configuration_error is not None:raise configuration_error
        from app.parent_pipeline.integration import execute
        return execute(profile=args.profile,run=args.run,gemini_cap=args.gemini_max_attempts,
                       cover_cap=args.cover_max_attempts,max_parents=args.max_parents,
                       headless=args.headless,
                       **({'best_effort': True} if args.best_effort else {}),
                       override_ids=args.override_review_parent_id,confirm_override=args.confirm_review_override)
    if args.run:
        from app.parent_pipeline.execution_log import invoke
        result=invoke(execute_run,profile=args.profile,headless=args.headless)
    else:
        try:result=execute_run()
        except (Exception,SystemExit):result=dict(status='failed',reason='discovery_or_configuration_failed')
    print(json.dumps(result,sort_keys=True))
    if args.run:
        print(result.get('execution',{}).get('summary','Instagram discovery: failed; execution record unavailable'),file=sys.stderr)
    return 0 if result['status'] in ('preflight','completed') else 1

if __name__=='__main__':raise SystemExit(main())
