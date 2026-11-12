"""Shared durable Gemini safety, updated only while the existing parent lock is held.

Legacy status-only records remain valid. Request history is never cleared by a
retry authorization. No prompts, source URLs, credentials or responses are saved.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from app.event_cover_service.openai_cover import atomic_write, durable_mkdir

DIRECTORY = Path(__file__).resolve().parents[2] / '.parent-pipeline-attempts'
_STATUSES = {'in_progress', 'review_required', 'retryable', 'committed'}
_STATES = {'intent', 'response_received', 'failed', 'uncertain', 'validation_failed', 'committed'}
_current = ContextVar('instagram_durable_gemini_attempt', default=None)


class GeminiReviewRequired(RuntimeError):
    def __init__(self):
        super().__init__('gemini_request_review_required')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load(parent_id):
    path = DIRECTORY / f'{parent_id}.json'
    if not path.exists():
        return dict(parent_id=parent_id, requests=[], history=[])
    try:
        data = json.loads(path.read_text())
        if (not isinstance(data, dict) or data.get('parent_id') != parent_id
                or data.get('status') not in _STATUSES
                or not isinstance(data.get('requests', []), list)
                or not isinstance(data.get('history', []), list)
                or any(not isinstance(r, dict) or r.get('state') not in _STATES
                       for r in data.get('requests', []))):
            raise ValueError()
        return data
    except Exception:
        raise GeminiReviewRequired() from None


def review_required(parent_id):
    try:
        data = _load(parent_id)
        return data.get('status') not in (None, 'retryable', 'committed') or (
            bool(data.get('requests')) and data.get('status') != 'committed')
    except GeminiReviewRequired:
        return True


def _save(parent_id, data, status):
    if status not in _STATUSES:
        raise ValueError('Invalid intent status')
    # Preserve a legacy record's original state before its first upgrade.
    if 'status' in data and not data.get('history'):
        data.setdefault('history', []).append(dict(status=data['status'], legacy=True))
    data.update(parent_id=parent_id, status=status, updated_at=_now(), version=2)
    data.setdefault('history', []).append(dict(status=status, timestamp=data['updated_at']))
    durable_mkdir(DIRECTORY)
    atomic_write(DIRECTORY / f'{parent_id}.json', json.dumps(data).encode())


def record(parent_id, status):
    data = _load(parent_id)
    if status == 'retryable' and data.get('requests'):
        status = 'review_required'  # Never reset evidence of a dispatched request.
    _save(parent_id, data, status)


def inspect(parent_id):
    """Allowlisted read-only summary; never return arbitrary journal content."""
    exists = (DIRECTORY / f'{parent_id}.json').exists()
    try:
        data = _load(parent_id)
        requests = data.get('requests', [])
        return dict(record_exists=exists, status=data.get('status', 'not_recorded'),
                    review_required=review_required(parent_id), request_count=len(requests),
                    last_request_state=requests[-1]['state'] if requests else None,
                    note='No record does not establish that no historical paid requests occurred.')
    except GeminiReviewRequired:
        return dict(record_exists=exists, status='unreadable', review_required=True)


class Attempt:
    def __init__(self, parent_id, *, authorize_retry=False):
        self.parent_id = parent_id
        self.authorize_retry = authorize_retry
        self.request_id = None

    def check_available(self):
        # A confirmed retry is consumed by ONE new request, even if it fails.
        if self.request_id is not None:
            raise GeminiReviewRequired()
        if review_required(self.parent_id) and not self.authorize_retry:
            raise GeminiReviewRequired()

    def before_dispatch(self, *, model=None):
        self.check_available()
        data = _load(self.parent_id)
        request_id, timestamp = uuid4().hex, _now()
        request = dict(request_id=request_id, parent_id=self.parent_id,
                       timestamp=timestamp, state='intent',
                       manual_retry_authorized=bool(self.authorize_retry),
                       events=[dict(state='intent', timestamp=timestamp)])
        from app.ai_analyze_service.model_selection import diagnostic_model
        selected_model=diagnostic_model(model)
        if selected_model is not None:
            request['model']=selected_model
        data.setdefault('requests', []).append(request)
        # fsync + atomic replace complete BEFORE the caller dispatches Gemini.
        _save(self.parent_id, data, 'in_progress')
        self.request_id = request_id

    def outcome(self, state):
        if self.request_id is None:
            return
        data = _load(self.parent_id)
        request = next(r for r in data['requests'] if r['request_id'] == self.request_id)
        request.update(state=state)
        request['events'].append(dict(state=state, timestamp=_now()))
        _save(self.parent_id, data, 'committed' if state == 'committed' else 'review_required')

    def finish(self, *, committed):
        if self.request_id is not None:
            if committed:
                self.outcome('committed')
            else:
                # Successful HTTP without committed extraction is also unsafe to
                # repeat automatically (invalid JSON or DB failure, for example).
                record(self.parent_id, 'review_required')


def current():
    return _current.get()


@contextmanager
def capture(attempt):
    token = _current.set(attempt)
    try:
        yield attempt
    finally:
        _current.reset(token)
