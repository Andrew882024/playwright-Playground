"""Read-only recovery preview. Never enables recovery or performs external work."""
import json
from pathlib import Path


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    from app.instagram_scraper.recency import scrape_cutoff
    from app.pipeline_recovery.service import RecoveryRun
    try:
        run=RecoveryRun.configured(scrape_cutoff())
        preview=run.preview()
        already_synchronized=preview.pop('already_synchronized',[])
        print(json.dumps(dict(status='dry-run',caps=run.caps,recovery_enabled=run.active,
                             candidates=preview,already_synchronized=already_synchronized),sort_keys=True))
        return 0
    except Exception:
        print(json.dumps(dict(status='error',reason='recovery_preview_unavailable_migration_or_configuration')))
        return 1

if __name__=='__main__':
    raise SystemExit(main())
