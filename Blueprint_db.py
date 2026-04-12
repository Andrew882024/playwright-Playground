"""SQLAlchemy models and session for PostgreSQL (see docker-compose.yml)."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# Defaults match docker-compose.yml and .env.example for local development.
db_username = os.getenv("DB_USERNAME", "playground")
db_password = os.getenv("DB_PASSWORD", "playground")
db_hostname = os.getenv("DB_HOSTNAME", "localhost")
db_port = os.getenv("DB_PORT", "5433")
db_database = os.getenv("DB_DATABASE", "playground")

db_url = f"postgresql+psycopg2://{db_username}:{db_password}@{db_hostname}:{db_port}/{db_database}"

engine = create_engine(db_url)

Base = declarative_base()

# Base.metadata.create_all(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Pulse(Base):
    """One row per scraped Instagram post (and optional AI enrichment)."""

    __tablename__ = "pulse"
    __table_args__ = (
        UniqueConstraint("scraped_profile", "shortcode", name="uq_pulse_profile_shortcode"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    scraped_profile = Column(String(255), nullable=False, index=True)
    shortcode = Column(String(32), nullable=False, index=True)
    permalink = Column(Text, nullable=False)
    from_username = Column(String(255), nullable=False)
    title = Column(Text, nullable=True)
    taken_at = Column(BigInteger, nullable=True)
    posted_at_utc = Column(DateTime(timezone=True), nullable=True)
    media_pk = Column(String(64), nullable=True)

    caption_text = Column(Text, nullable=True)
    thread_comments_text = Column(Text, nullable=True)

    comment_count_total = Column(Integer, nullable=True)
    comments_incomplete = Column(Boolean, nullable=True)

    primary_media_url = Column(Text, nullable=True)
    extra_media_urls = Column(Text, nullable=True)

    is_event = Column(Boolean, nullable=True)
    event_title = Column(Text, nullable=True)
    provider_name = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    duration = Column(Text, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    event_description = Column(Text, nullable=True)
    confidence = Column(String(16), nullable=True)
    raw_notes = Column(Text, nullable=True)
    gemini_model = Column(String(128), nullable=True)

    event_start_year = Column(Integer, nullable=True)
    event_start_month = Column(Integer, nullable=True)
    event_start_day = Column(Integer, nullable=True)
    event_start_hour = Column(Integer, nullable=True)
    event_start_minute = Column(Integer, nullable=True)
    event_start_tz_iana = Column(String(128), nullable=True)

    event_end_year = Column(Integer, nullable=True)
    event_end_month = Column(Integer, nullable=True)
    event_end_day = Column(Integer, nullable=True)
    event_end_hour = Column(Integer, nullable=True)
    event_end_minute = Column(Integer, nullable=True)
    event_end_tz_iana = Column(String(128), nullable=True)

    row_created_at = Column(DateTime(timezone=True), server_default=func.now())
    row_updated_at = Column(DateTime(timezone=True), onupdate=func.now())
