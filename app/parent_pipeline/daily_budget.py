"""Durable single-host paid-request reservations; never refunds uncertain work."""
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

PROVIDERS = ('gemini', 'openai_cover')
ZONE = ZoneInfo('America/Los_Angeles')
_current = ContextVar('instagram_daily_paid_budget', default=None)


class BudgetStopped(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def now():
    return datetime.now(timezone.utc)


def day_at(timestamp):
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BudgetStopped('daily_paid_ledger_unavailable')
    return timestamp.astimezone(ZONE).date().isoformat()


class Ledger:
    def __init__(self, directory=None, limits=None):
        self.directory = Path(directory or os.getenv('INSTAGRAM_PAID_REQUEST_LEDGER_DIR') or
                              Path.home() / '.local/state/event-master/instagram-paid-requests').expanduser()
        self.limits = limits if limits is not None else {
            'gemini': self._limit('INSTAGRAM_DAILY_GEMINI_MAX_REQUESTS'),
            'openai_cover': self._limit('INSTAGRAM_DAILY_COVER_MAX_REQUESTS')}
        if set(self.limits) != set(PROVIDERS) or any(type(v) is not int or v < 0 for v in self.limits.values()):
            raise BudgetStopped('daily_paid_ledger_unavailable')
        self.blocked_reason = None

    @staticmethod
    def _limit(name):
        try:
            raw = (os.getenv(name) or '').strip()
            value = int(raw) if raw else 1
            if value < 0: raise ValueError()
            return value
        except ValueError:
            raise BudgetStopped('daily_paid_ledger_unavailable') from None

    @contextmanager
    def _locked(self, *, create):
        fd = None
        try:
            if not self.directory.is_absolute() or self.directory.is_symlink():
                raise ValueError()
            if create:
                missing=[]
                cursor=self.directory
                while not cursor.exists():
                    missing.append(cursor)
                    cursor=cursor.parent
                for directory in reversed(missing):
                    try: directory.mkdir(mode=0o700)
                    except FileExistsError: pass
                    parent_fd=os.open(directory.parent,os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(parent_fd)
                    finally: os.close(parent_fd)
            if not self.directory.exists():
                yield None
                return
            lock = self.directory / 'ledger.lock'
            created = False
            if create:
                try:
                    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    created = True
                    os.fsync(fd)
                    self._sync_dir()
                except FileExistsError:
                    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
            else:
                if not lock.exists():
                    # A directory without a ledger/lock is safe to inspect, never to reset.
                    if (self.directory / 'ledger.json').exists(): raise ValueError()
                    yield None
                    return
                fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise TimeoutError()
                    time.sleep(0.01)
            path = self.directory / 'ledger.json'
            if path.is_symlink(): raise ValueError()
            if not path.exists():
                if not created: raise ValueError()  # Missing initialized state must never reset usage.
                data = dict(version=1, days={})
            else:
                with open(path) as stream: data = json.load(stream)
                self._validate(data)
            yield data
        except BudgetStopped:
            raise
        except Exception:
            raise BudgetStopped('daily_paid_ledger_unavailable') from None
        finally:
            if fd is not None: os.close(fd)

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('days'), dict):
            raise ValueError()
        for day, entry in data['days'].items():
            if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day: raise ValueError()
            if set(entry) != set(PROVIDERS): raise ValueError()
            for provider in PROVIDERS:
                row = entry[provider]
                if type(row['limit']) is not int or row['limit'] < 0 or not isinstance(row['reservations'], list):
                    raise ValueError()
                for item in row['reservations']:
                    if (type(item['parent_id']) is not int or item['parent_id'] <= 0 or
                            day_at(datetime.fromisoformat(item['timestamp'])) != day or
                            len(item['id']) != 32 or item['state'] != 'reserved'):
                        raise ValueError()

    def _sync_dir(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def _save(self, data):
        fd, name = tempfile.mkstemp(prefix='.ledger-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / 'ledger.json')
            self._sync_dir()
        finally:
            if os.path.exists(name): os.unlink(name)

    def reserve(self, provider, parent_id):
        if provider not in PROVIDERS or type(parent_id) is not int or parent_id <= 0:
            raise BudgetStopped('daily_paid_ledger_unavailable')
        try:
            with self._locked(create=True) as data:
                timestamp = now()  # Determine the LA day after acquiring the lock, including midnight waits.
                day = day_at(timestamp)
                entry = data['days'].setdefault(day, {p:dict(limit=self.limits[p], reservations=[]) for p in PROVIDERS})
                row = entry[provider]
                row['limit'] = min(row['limit'], self.limits[provider])
                if len(row['reservations']) >= row['limit']:
                    self._save(data)  # Persist a lowered limit even on denial.
                    raise BudgetStopped(f'daily_{provider}_budget_exhausted')
                row['reservations'].append(dict(id=uuid4().hex, parent_id=parent_id,
                    timestamp=timestamp.isoformat(), state='reserved'))
                self._save(data)
        except BudgetStopped as exc:
            self.blocked_reason = exc.reason
            raise

    def summary(self):
        try:
            with self._locked(create=False) as data:
                day = day_at(now())
                entry = (data or {}).get('days', {}).get(day, {})
                providers = {}
                for provider in PROVIDERS:
                    row = entry.get(provider, {})
                    limit = min(row.get('limit', self.limits[provider]), self.limits[provider])
                    used = len(row.get('reservations', []))
                    providers[provider] = dict(limit=limit, used=used, remaining=max(0,limit-used))
                return dict(status='available', day=day, timezone=str(ZONE), providers=providers,
                            blocked_reason=self.blocked_reason)
        except BudgetStopped:
            return dict(status='unavailable', reason='daily_paid_ledger_unavailable')


def current():
    return _current.get()


@contextmanager
def capture(ledger):
    token = _current.set(ledger)
    try: yield ledger
    finally: _current.reset(token)


def reserve(provider, parent_id):
    ledger = current()
    if ledger is not None: ledger.reserve(provider, parent_id)


def scoped(function):
    """Share a ledger context through discovery -> parent, without any import-time I/O."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        ledger = current()
        try:
            ledger = ledger or Ledger()
        except BudgetStopped:
            return dict(status='review_required', reason='daily_paid_ledger_unavailable',
                        daily_budget=dict(status='unavailable', reason='daily_paid_ledger_unavailable'))
        token = _current.set(ledger)
        try:
            result = function(*args, **kwargs)
            result['daily_budget'] = ledger.summary()
            return result
        finally:
            _current.reset(token)
    return wrapped
