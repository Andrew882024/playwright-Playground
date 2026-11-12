#!/usr/bin/env python3
"""Prepare up to five staged candidates. No execution/paid/import option exists."""
import argparse,json
from collect_instagram_posts import STAGING
from app.instagram_scraper.analysis_batch import prepare_batch
from app.instagram_scraper.collection_staging import atomic,private_dir


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit',type=int,choices=range(1,6),default=5)
    p.add_argument('--save',action='store_true',help='Save a private local review manifest only')
    args=p.parse_args(argv)
    try:
        result=prepare_batch(STAGING,limit=args.limit)
        if args.save:
            directory=private_dir(STAGING/'analysis-batches');path=directory/(result['batch_id']+'.json')
            atomic(path,result);result['manifest_path']=str(path)
    except Exception as exc:result=dict(status='failed',reason='analysis_batch_preparation_failed',exception_class=type(exc).__name__)
    print(json.dumps(result,sort_keys=True));return int(result['status']=='failed')


if __name__=='__main__':raise SystemExit(main())
