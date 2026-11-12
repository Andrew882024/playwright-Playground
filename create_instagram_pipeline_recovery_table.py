"""Explicit idempotent migration: create only the recovery-state table."""
def main():
    from Blueprint_db import engine, InstagramPipelineRecovery
    InstagramPipelineRecovery.__table__.create(bind=engine, checkfirst=True)
    print('Instagram recovery-state table is ready.')

if __name__ == '__main__':
    main()
