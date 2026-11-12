"""SQLAlchemy models and session for PostgreSQL (see docker-compose.yml)."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# Defaults match docker-compose.yml and .env.example for local development.
db_username = os.getenv("DB_USERNAME", "playground")
db_password = os.getenv("DB_PASSWORD", "playground")
db_hostname = os.getenv("DB_HOSTNAME", "localhost")
db_port = os.getenv("DB_PORT", "5439")
db_database = os.getenv("DB_DATABASE", "playground")

db_url = f"postgresql+psycopg2://{db_username}:{db_password}@{db_hostname}:{db_port}/{db_database}"

engine = create_engine(db_url, connect_args={
    "connect_timeout": 10,
    "options": "-c statement_timeout=120000 -c lock_timeout=15000",
})

Base = declarative_base()

# Base.metadata.create_all(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class InstagramPosts(Base):
    """One row per scraped Instagram post (and optional AI enrichment)."""

    __tablename__ = "instagram_posts"
    __table_args__ = (
        UniqueConstraint(
            "profile_username",
            "post_shortcode",
            name="uq_instagram_posts_profile_username_post_shortcode",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    profile_username = Column(String(255), nullable=False, index=True)
    post_shortcode = Column(String(32), nullable=False, index=True)
    post_url = Column(Text, nullable=False)
    post_title = Column(Text, nullable=True)
    posted_unix_seconds = Column(BigInteger, nullable=True)
    posted_time = Column(DateTime(timezone=True), nullable=True)
    instagram_media_id = Column(String(64), nullable=True)
    caption = Column(Text, nullable=True)
    comments_json = Column(JSONB, nullable=True)
    main_image_url = Column(Text, nullable=True)
    additional_image_urls = Column(JSONB, nullable=True)

    is_event = Column(Boolean, nullable=True)
    event_title = Column(Text, nullable=True)
    provider_name = Column(Text, nullable=True)
    post_description = Column(Text, nullable=True)
    location = Column(Text, nullable=True)
    duration_in_minutes = Column(Integer, nullable=True)
    confidence = Column(String(16), nullable=True)
    ai_model = Column(String(128), nullable=True)
    ai_analyzed = Column(Boolean,nullable=False, server_default=false())
    event_start_at = Column(DateTime(timezone=True), nullable=True)
    is_duplicated = Column(Boolean, nullable=True)
    event_end_at = Column(DateTime(timezone=True), nullable=True)

    own_s3_url_for_main_image = Column(Text, nullable=True)

    # Parent aggregate presentation/cover; legacy child cover fields stay intact.
    collection_title = Column(Text, nullable=True)
    generated_image_url = Column(Text, nullable=True)
    generated_image_input_hash = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class InstagramEvents(Base):
    """Zero or more events per post; original images stay on the parent."""

    __tablename__ = "instagram_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instagram_post_id = Column(
        Integer, ForeignKey("instagram_posts.id"), nullable=False, index=True
    )
    event_title = Column(Text, nullable=False)
    provider_name = Column(Text, nullable=True)
    event_description = Column(Text, nullable=True)
    location = Column(Text, nullable=True)
    event_start_at = Column(DateTime(timezone=True), nullable=True)
    event_end_at = Column(DateTime(timezone=True), nullable=True)
    duration_in_minutes = Column(Integer, nullable=True)
    confidence = Column(String(16), nullable=True)
    is_duplicated = Column(Boolean, nullable=True)
    generated_image_url = Column(Text, nullable=True)
    generated_image_input_hash = Column(String(64), nullable=True)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now()
    )


class InstagramPipelineRecovery(Base):
    __tablename__ = 'instagram_pipeline_recovery'
    instagram_post_id = Column(Integer, ForeignKey('instagram_posts.id'), primary_key=True)
    stage = Column(String(16), primary_key=True)
    source_revision = Column(String(64), nullable=False)
    acknowledged_revision = Column(String(64), nullable=True)
    status = Column(String(24), nullable=False, default='pending')
    attempt_count = Column(Integer, nullable=False, default=0)
    failure_count = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    safe_reason = Column(String(64), nullable=True)
    lease_token = Column(String(32), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
