#!/usr/bin/env python3
"""Explicit READ ONLY canonical/processed-post snapshot. No importer execution."""
import argparse
from datetime import datetime,timedelta,timezone
import html,json,re
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def snapshot():
    from dotenv import dotenv_values
    import psycopg2,pymysql
    from app.instagram_event_master_projection import parent_marker
    from app.instagram_scraper.collection_staging import atomic,private_dir
    from app.instagram_scraper.daily_pilot import single_run_lock
    cfg=dotenv_values(ROOT/'.env')
    backend=Path(cfg.get('EVENT_MASTER_BACKEND_DIR') or '/home/zoewu/cssa/cssa-activity-backend')
    mysql=dotenv_values(backend/'.env')
    if any(str(c.get('DB_HOSTNAME','localhost')).lower() not in ('localhost','127.0.0.1','::1') for c in (cfg,mysql)):
        raise ValueError('Only local read-only sources supported')
    pg=None;my=None
    try:
        pg=psycopg2.connect(host=cfg.get('DB_HOSTNAME') or 'localhost',port=cfg.get('DB_PORT') or '5439',
            dbname=cfg.get('DB_DATABASE') or 'playground',user=cfg.get('DB_USERNAME') or 'playground',
            password=cfg.get('DB_PASSWORD') or 'playground',connect_timeout=5,
            options='-c statement_timeout=5000 -c default_transaction_read_only=on')
        pg.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with pg.cursor() as cur:
            cur.execute('SELECT p.id,p.profile_username,p.post_shortcode,p.post_url,p.ai_analyzed,'
                'EXISTS(SELECT 1 FROM instagram_events e WHERE e.instagram_post_id=p.id),'
                "COALESCE(p.generated_image_url,'')<>'' FROM instagram_posts p "
                'WHERE COALESCE(p.posted_time,to_timestamp(p.posted_unix_seconds))>=%s ORDER BY p.id LIMIT 10001',
                (datetime.now(timezone.utc)-timedelta(days=40),))
            parents=cur.fetchall()
        if len(parents)>10000:raise ValueError('Snapshot bound exceeded')
        my=pymysql.connect(host=mysql['DB_HOSTNAME'],port=int(mysql['DB_PORT']),user=mysql['DB_USERNAME'],
            password=mysql['DB_PASSWORD'],database=mysql['DB_DATABASE'],connect_timeout=5,read_timeout=5,write_timeout=5,autocommit=False)
        with my.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY')
            cur.execute('SELECT id,event_description FROM events WHERE LOCATE(%s,event_description)>0 LIMIT 10001',('[eventmaster-import:instagram:playground:instagram_',))
            events=cur.fetchall()
        if len(events)>10000:raise ValueError('Snapshot bound exceeded')
        lookup={p[0]:p for p in parents};imports=[];legacy=set()
        for eid,desc in events:
            desc=desc or ''
            if '[eventmaster-import:instagram:playground:instagram_events:' in desc:
                legacy.update(re.findall(r'Post shortcode: ([A-Za-z0-9_-]{1,32})<br />',desc))
            markers=re.findall(r'\[eventmaster-import:instagram:playground:instagram_posts:([0-9]+)\]',desc)
            for raw in markers:
                p=lookup.get(int(raw))
                if p is None:continue
                pid,account,code,url,*_=p
                if (len(markers)!=1 or f'Post shortcode: {html.escape(code)}<br />' not in desc or
                        f'Post URL: {html.escape(url,quote=True)}<br />' not in desc):
                    legacy.add(code);continue
                imports.append(dict(parent_id=pid,account=account,shortcode=code,event_id=eid,marker=parent_marker(pid)))
        data=dict(version=1,captured_at=datetime.now(timezone.utc).isoformat(),imports=imports,
            legacy_shortcodes=sorted(legacy),processed=[dict(parent_id=p[0],account=p[1],shortcode=p[2],
                analyzed=bool(p[4]),has_activities=bool(p[5]),has_parent_cover=bool(p[6])) for p in parents],
            authority='read_only_snapshot_negative_guard_only')
        staging=private_dir(ROOT/'.instagram-post-staging')
        with single_run_lock(staging) as held:
            if not held:raise ValueError('Collection busy')
            atomic(staging/'canonical_imports.json',data)
        return dict(status='snapshot_saved',canonical_imports=len(imports),legacy_conflicts=len(legacy),
                    stored_parents=len(parents),database_writes=0,paid_requests=0)
    finally:
        for conn in (pg,my):
            if conn is not None:conn.rollback();conn.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--refresh',action='store_true');args=p.parse_args(argv)
    if not args.refresh:result=dict(status='preflight',database_operations=0,note='--refresh performs SELECTs and writes only a private local snapshot')
    else:
        try:result=snapshot()
        except Exception as exc:result=dict(status='failed',reason='read_only_snapshot_unavailable',exception_class=type(exc).__name__)
    print(json.dumps(result,sort_keys=True));return int(result['status']=='failed')


if __name__=='__main__':raise SystemExit(main())
