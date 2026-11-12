#!/usr/bin/env python3
"""Inspect local review queue, or record explicit review intent. Never pay/import."""
import argparse,json
from app.instagram_scraper.collection_review import build_queue,record_review,configured_accounts
from collect_instagram_posts import STAGING


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',choices=configured_accounts())
    p.add_argument('--request-approval',help='Candidate fingerprint; records intent, not a trust override')
    p.add_argument('--revision')
    p.add_argument('--confirm-review',action='store_true')
    p.add_argument('--details',action='store_true',help='Include safe candidate IDs/reasons, never captions/image URLs')
    args=p.parse_args(argv)
    if args.request_approval:
        if not args.confirm_review or not args.profile or not args.revision:p.error('Explicit profile/revision and separate confirmation required')
        result=record_review(STAGING,args.request_approval,args.profile,args.revision)
    else:
        result=build_queue(STAGING)
        if args.profile:
            result['records']=[r for r in result['records'] if r['requested_profile']==args.profile]
        if not args.details:result.pop('records',None)
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=='__main__':raise SystemExit(main())
