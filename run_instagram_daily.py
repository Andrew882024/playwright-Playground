"""Disabled daily pilot. Default preflight is local; capture validation is zero-paid."""
import argparse
from contextlib import redirect_stdout,redirect_stderr
import json
from pathlib import Path
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=['dvcatucsd'],default='dvcatucsd')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight',action='store_true')
    modes.add_argument('--validate-capture',action='store_true')
    modes.add_argument('--run',action='store_true')
    parser.add_argument('--gemini-max-attempts',type=int,choices=(0,1),default=0)
    parser.add_argument('--cover-max-attempts',type=int,choices=(0,1),default=0)
    parser.add_argument('--max-scrolls',type=int,choices=(0,1,2),default=1)
    args=parser.parse_args(argv)
    if args.gemini_max_attempts<0 or args.cover_max_attempts<0:
        parser.error('Request limits must be nonnegative')
    if args.validate_capture and (args.gemini_max_attempts or args.cover_max_attempts):
        parser.error('Capture validation requires both request limits to be zero')
    class Quiet:
        def write(self,value):return len(value)
        def flush(self):pass
    try:
        with redirect_stdout(Quiet()),redirect_stderr(Quiet()):
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parent/'.env')
            from app.instagram_scraper.daily_pilot import execute
            result=execute(validate_capture=args.validate_capture,run=args.run,profile=args.profile,
                gemini_cap=args.gemini_max_attempts,cover_cap=args.cover_max_attempts,max_scrolls=args.max_scrolls)
    except (Exception,SystemExit):
        result=dict(status='failed',reason='daily_pilot_configuration_unavailable')
    print(json.dumps(result,sort_keys=True))
    print('Instagram daily pilot: '+result['status'],file=sys.stderr)
    return 0 if result['status'] in ('preflight','disabled','completed_with_skips') else 78 if result['status']=='blocked' else 1


if __name__=='__main__':raise SystemExit(main())
