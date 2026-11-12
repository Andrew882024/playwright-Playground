"""Run-local Gemini dispatch accounting; no persistence or external operations."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock


class GeminiRequestStopped(RuntimeError):
    """Fixed safe message; provider exception details stay in diagnostics only."""


@dataclass
class GeminiHttpBudget:
    limit: int | None = None
    dispatched: int = 0
    stopped_reason: str | None = None

    def __post_init__(self):
        if self.limit is not None and (type(self.limit) is not int or self.limit < 0):
            raise ValueError('Gemini HTTP limit must be a nonnegative integer.')
        self._lock = Lock()

    @property
    def strict(self):
        return self.limit is not None

    def reserve(self):
        with self._lock:
            self._check()
            # Reserve before SDK dispatch, including requests with unknown outcomes.
            self.dispatched += 1

    def _check(self):
        if self.stopped_reason is not None:
            raise GeminiRequestStopped(self.stopped_reason)
        if self.strict and self.dispatched >= self.limit:
            self.stopped_reason = 'gemini_http_budget_exhausted'
            raise GeminiRequestStopped(self.stopped_reason)

    def check_available(self):
        with self._lock:
            self._check()

    def stop_failed_request(self, *, response_received):
        with self._lock:
            self.stopped_reason = ('gemini_http_budget_exhausted' if self.dispatched >= self.limit else
                                   'gemini_http_request_failed' if response_received else
                                   'gemini_http_outcome_uncertain')
        raise GeminiRequestStopped(self.stopped_reason) from None


_current = ContextVar('instagram_gemini_http_budget', default=None)


def current():
    return _current.get()


@contextmanager
def capture(budget):
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)
