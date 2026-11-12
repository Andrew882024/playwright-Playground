"""Explicit database-free schema capture with interactive review; default does nothing live."""
from contextlib import redirect_stdout,redirect_stderr,nullcontext
import argparse
import json
import os
from pathlib import Path
import sys


class Quiet:
    def write(self,value):return len(value)
    def flush(self):pass


def save_fixture(path,fixture):
    # Exclusive creation prevents accidental overwrite of existing evidence.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(fixture,stream,sort_keys=True,indent=2)
        stream.write('\n');stream.flush();os.fsync(stream.fileno())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True,help='Exactly one configured Instagram account')
    parser.add_argument('--headless',action='store_true')
    parser.add_argument('--capture',action='store_true',help='Authorize one browser-only capture; no database or downstream stages')
    parser.add_argument('--partial-capture',action='store_true',help='Count visible candidates only; always partial_unverified, no review or fixture')
    parser.add_argument('--review',action='store_true',help='Review candidates on an interactive terminal after capture')
    parser.add_argument('--fixture',type=Path,help='New output file; only individually confirmed schema fields are retained')
    args=parser.parse_args(argv)
    if args.partial_capture and (not args.capture or args.review or args.fixture):
        parser.error('--partial-capture requires --capture and cannot be combined with review/fixture')
    if bool(args.fixture)!=args.review:parser.error('--review and --fixture are required together')
    if args.review and not args.capture:parser.error('--review requires --capture')
    if args.review and (not sys.stdin.isatty() or not sys.stderr.isatty()):
        parser.error('Interactive terminal required for review')
    if args.fixture and (args.fixture.exists() or args.fixture.is_symlink() or not args.fixture.parent.is_dir()):
        parser.error('Fixture must be a new file in an existing directory')
    if not args.capture:
        print(json.dumps(dict(status='inspection_only',mode='capture_only',database='not_used',live_capture=False,
                              gemini_max_attempts=0,cover_max_attempts=0)))
        return 0
    from app.instagram_scraper.schema_probe import Probe,enabled
    from app.instagram_scraper.schema_capture import capture
    probe=Probe()
    try:
        with (nullcontext() if args.partial_capture else enabled(probe)),redirect_stdout(Quiet()),redirect_stderr(Quiet()):
            options={'partial_capture':True} if args.partial_capture else {}
            result=capture(args.profile,headless=args.headless,**options)
        print(json.dumps(result,sort_keys=True))
        code=0 if result['status']=='completed' else 1
        if not args.partial_capture:print(json.dumps(probe.summary(),sort_keys=True))
        if args.review:
            def confirm(field,placement):
                print(f'Schema candidate: {field} ({placement}). Query evidence does not prove harmlessness.',file=sys.stderr)
                print('Retain this field after checking it contains no private information? Type its exact name, or Enter to omit:',file=sys.stderr)
                return sys.stdin.readline().rstrip('\r\n')==field
            fixture=probe.review(confirm)
            save_fixture(args.fixture,fixture)
            print(json.dumps(dict(schema_fixture='saved',reviewed_fields=len(fixture['reviewed_fields']))))
        return code
    except KeyboardInterrupt:
        print(json.dumps(dict(status='partial_unverified',capture_outcome='interrupted') if args.partial_capture
                         else dict(schema_probe='interrupted')));return 1
    except Exception:
        print(json.dumps(dict(status='partial_unverified',capture_outcome='failed',reason='diagnostic_execution_failed')
                         if args.partial_capture else dict(schema_probe='failed',reason='diagnostic_execution_failed')));return 1


if __name__=='__main__':raise SystemExit(main())
