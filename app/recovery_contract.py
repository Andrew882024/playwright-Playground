"""Allowlisted read-only recovery DTOs. No URLs, credentials or arbitrary metadata."""
import hashlib
import html
import json
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute


class SafeReadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Default validation responses echo arbitrary input, including URLs.
                raise HTTPException(422, 'Invalid read-only query or path parameters.') from None
        return safe_handler


def safe_text(value):
    value = re.sub(r'https?://[^\s<>"\']+', '[link omitted]', value, flags=re.I)
    value = re.sub(r'<[^>]*>', ' ', value)
    return html.escape(re.sub(r'\s+', ' ', value).strip(), quote=True)[:2000]


Text = Annotated[str, AfterValidator(safe_text)]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Stage = Literal['cover', 'event_master']
Status = Literal['pending', 'succeeded', 'failed', 'review_required']
OWNED_FIELDS = frozenset(('event_title','event_description','event_provider_name','showed_event_provider_name',
    'event_location','event_start_date_and_time','event_duration_in_minutes','use_undetermined_time',
    'undetermined_time_text','event_imageUrl'))


def field_hash(value):
    if isinstance(value, datetime): value = value.isoformat()
    if isinstance(value, float) and value.is_integer(): value = int(value)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


class DTO(BaseModel):
    model_config = ConfigDict(extra='ignore')


class Health(DTO):
    status: Literal['ok'] = 'ok'
    read_only: Literal[True] = True
    recovery_schema_ready: bool


class Configuration(DTO):
    read_only: Literal[True] = True
    generate_covers_enabled: bool
    sync_event_master_enabled: bool
    recover_covers_enabled: bool
    recover_event_master_enabled: bool
    cover_attempt_cap: int = Field(ge=0)
    cover_recovery_cap: int = Field(ge=0)
    import_recovery_cap: int = Field(ge=0)
    max_failures: int = Field(ge=1)


class Task(DTO):
    parent_id: int = Field(gt=0)
    stage: Stage
    status: Status
    attempt_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    lease_active: bool
    acknowledgment: Literal['not_acknowledged','current','stale','unknown']
    reason: Literal['none','manual_review','operation_failed','retry_limit','skipped_expired',
                    'activity_date_review_required','stale_revision','paid_budget_exhausted','other']


class Tasks(DTO):
    items: list[Task]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1,le=100)


class Activity(DTO):
    id: int = Field(gt=0)
    title: Text
    provider: Text
    location: Text
    start: datetime | None
    end: datetime | None
    duration_in_minutes: float | None


class Parent(DTO):
    parent_id: int = Field(gt=0)
    title: Text
    provider: Text
    description: Text
    location: Text
    earliest_start: datetime | None
    multi_session: bool
    activities: list[Activity]
    image_source: Literal['generated_parent','original_s3','original','unavailable']
    source_revision: Digest
    recovery: list[Task]


class ImportPreview(DTO):
    parent: Parent
    projection_status: Literal['ready','no_activities','invalid_source']
    expected_field_hashes: dict[str,Digest]
    legacy_provenance_hashes: list[Digest]

    @field_validator('expected_field_hashes')
    @classmethod
    def valid_fields(cls, value):
        if value and set(value) != OWNED_FIELDS:
            raise ValueError('Unexpected projection field set.')
        return value

    @model_validator(mode='after')
    def consistent_projection(self):
        if self.projection_status == 'ready' and set(self.expected_field_hashes) != OWNED_FIELDS:
            raise ValueError('Missing projection hashes.')
        if self.projection_status != 'ready' and self.expected_field_hashes:
            raise ValueError('Unexpected projection hashes.')
        return self
