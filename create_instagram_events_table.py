"""Explicitly create only instagram_events; the parent table must already exist."""

from Blueprint_db import InstagramEvents, engine


def main() -> None:
    InstagramEvents.__table__.create(bind=engine, checkfirst=True)
    print("instagram_events table is ready.")


if __name__ == "__main__":
    main()
