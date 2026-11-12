"""Nonblocking PostgreSQL session locks. No schema creation or row writes."""
from contextlib import contextmanager
from sqlalchemy import text


@contextmanager
def advisory_lock(namespace,identifier):
    from Blueprint_db import engine
    with engine.connect() as connection:
        held=False
        try:
            held=connection.execute(text('SELECT pg_try_advisory_lock(:namespace,:identifier)'),
                dict(namespace=namespace,identifier=identifier)).scalar() is True
            connection.commit()
            yield held
        finally:
            if held:
                try:
                    connection.execute(text('SELECT pg_advisory_unlock(:namespace,:identifier)'),
                        dict(namespace=namespace,identifier=identifier))
                    connection.commit()
                except Exception:
                    connection.invalidate()
