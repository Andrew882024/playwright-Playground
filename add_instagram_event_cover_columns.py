"""Explicit idempotent migration; run manually against an existing child table."""


def main() -> None:
    from sqlalchemy import text
    from Blueprint_db import engine

    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE instagram_events "
            "ADD COLUMN IF NOT EXISTS generated_image_url TEXT, "
            "ADD COLUMN IF NOT EXISTS generated_image_input_hash VARCHAR(64)"
        ))
    print("Generated-cover columns are ready.")


if __name__ == '__main__':
    main()
