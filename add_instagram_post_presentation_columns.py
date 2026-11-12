"""Explicit idempotent PostgreSQL migration; no work occurs on import.

Run manually: python add_instagram_post_presentation_columns.py
Uses the Playground DB_* configuration. Adds only nullable parent columns;
never copies child covers, backfills values, or alters any child artifacts.
"""


def main():
    from sqlalchemy import text
    from Blueprint_db import engine
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE instagram_posts "
                "ADD COLUMN IF NOT EXISTS collection_title TEXT, "
                "ADD COLUMN IF NOT EXISTS generated_image_url TEXT, "
                "ADD COLUMN IF NOT EXISTS generated_image_input_hash VARCHAR(64)"
            ))
        print('Parent presentation/cover columns are ready.')
    finally:
        engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Parent column migration failed; check PostgreSQL configuration/schema. Details withheld.') from None
