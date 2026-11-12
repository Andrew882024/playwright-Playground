"""Inspect or explicitly generate/resume a cover for exactly one child event."""
import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child-id', type=int, required=True, help='instagram_events.id (not the parent post ID)')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true', help='Read-only inspection; no download, OpenAI, S3 or DB writes (default)')
    modes.add_argument('--generate', action='store_true', help='Authorize at most one paid generation; reuse existing artifacts first')
    modes.add_argument('--resume-only', action='store_true', help='Retry saved upload/publication only; never call OpenAI')
    args = parser.parse_args(argv)
    if args.child_id <= 0:
        parser.error('--child-id must be a positive integer')
    from dotenv import load_dotenv
    from app.event_cover_service.openai_cover import PROJECT_ROOT, CoverError, Settings, run_cover
    load_dotenv(PROJECT_ROOT / '.env')
    try:
        from Blueprint_db import SessionLocal
        result = run_cover(SessionLocal, args.child_id, Settings.from_env(),
                           dry_run=not (args.generate or args.resume_only), allow_generation=args.generate)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except CoverError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # External exceptions can include signed URLs or credentials. Do not echo them.
        print('Cover operation failed. Artifacts are retained; rerun inspection/resume after fixing configuration or storage. '
              'An uncertain OpenAI request must be reviewed manually; it is never automatically retried.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
