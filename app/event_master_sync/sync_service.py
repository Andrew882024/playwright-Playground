"""Delegate parent aggregate imports and legacy child recovery to the backend's own environment.

No importer module is loaded here. Never forward Playground database settings,
Python import paths, API keys or AWS credentials into the backend subprocess.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import re
from app.pipeline_recovery import hooks
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_SAFE_IMPORTER_ERRORS = {
    b'Set INSTAGRAM_DB_PASSWORD for noninteractive use.': 'source_password_missing',
    b'Required MySQL DB_* configuration is missing.': 'mysql_configuration_missing',
    b'MySQL DB_HOSTNAME must be a local loopback host.': 'mysql_host_not_local',
    b'Another import for this child is running; try later.': 'import_lock_unavailable',
    b'Another import for this parent or child is running; try later.': 'import_lock_unavailable',
}


def _failure_reason(stderr: bytes | None) -> str:
    # Approve only the entire message, allowing its terminal line ending. Never
    # expose unknown output or match a safe substring inside arbitrary stderr.
    if not isinstance(stderr, bytes):
        return 'importer_failed'
    return _SAFE_IMPORTER_ERRORS.get(stderr.rstrip(b'\r\n'), 'importer_failed')


class SyncConfigurationError(ValueError):
    """Only safe fixed messages may be exposed to operators."""


@dataclass(frozen=True)
class SyncSettings:
    backend_dir: Path
    python: Path
    timeout: float = 60

    @property
    def importer(self) -> Path:
        return self.backend_dir / 'import_instagram_event.py'

    @classmethod
    def from_env(cls) -> SyncSettings:
        backend = Path(os.getenv('EVENT_MASTER_BACKEND_DIR') or
                       Path.home() / 'cssa/cssa-activity-backend').expanduser()
        python = Path(os.getenv('EVENT_MASTER_IMPORT_PYTHON') or backend / 'venv/bin/python').expanduser()
        try:
            timeout = float(os.getenv('EVENT_MASTER_IMPORT_TIMEOUT_SECONDS') or '60')
        except ValueError:
            raise SyncConfigurationError('Importer timeout must be a finite positive number.') from None
        if not backend.is_absolute() or not python.is_absolute():
            raise SyncConfigurationError('Backend directory and interpreter must be absolute paths.')
        # Do NOT resolve the interpreter symlink: doing so can bypass its virtualenv.
        return cls(backend.resolve(), python, timeout)

    def validate(self) -> None:
        if not self.backend_dir.is_absolute() or not self.python.is_absolute():
            raise SyncConfigurationError('Backend directory and interpreter must be absolute paths.')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise SyncConfigurationError('Importer timeout must be a finite positive number.')
        if (not self.backend_dir.is_dir() or not self.importer.is_file()
                or not self.python.is_file() or not os.access(self.python, os.X_OK)):
            raise SyncConfigurationError('Backend directory, importer or executable interpreter is unavailable.')


def isolated_import_environment() -> dict[str, str]:
    # Allowlist instead of trying to enumerate every credential variable.
    names = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'TEMP', 'TMP')
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    # Backend load_dotenv() loads its own DB_* and INSTAGRAM_DB_PASSWORD. stdin is
    # DEVNULL, so missing source credentials cause a safe failure, never a prompt.
    return env


def children_for_parents(parent_ids: set[int]) -> list[int]:
    """All children of only current-run committed parents, regardless of covers."""
    if not parent_ids:
        return []
    from sqlalchemy import select
    from Blueprint_db import InstagramEvents, SessionLocal
    with SessionLocal() as session:
        return list(session.scalars(
            select(InstagramEvents.id)
            .where(InstagramEvents.instagram_post_id.in_(sorted(parent_ids)))
            .order_by(InstagramEvents.id.asc())
        ).all())


def empty_result() -> dict:
    return dict(status='completed', attempted=0, succeeded=0, failed=0, children=[])


def sync_children(child_ids: Iterable[int], settings: SyncSettings | None = None) -> dict:
    """Explicit sync/recovery: one isolated process per distinct child, no retries.

    Success includes new imports, unchanged existing imports and image updates.
    The importer alone decides these outcomes. Only approved stderr messages are
    classified; raw output is never returned or logged.
    """
    result = empty_result()
    ids = list(child_ids)
    if any(type(child_id) is not int or child_id <= 0 for child_id in ids):
        result.update(status='error', error='invalid_child_ids')
        return result
    ids = sorted(set(ids))
    if not ids:
        return result
    try:
        settings = settings or SyncSettings.from_env()
        settings.validate()
        env = isolated_import_environment()
    except Exception:
        result.update(status='error', error='importer_configuration_invalid')
        return result
    for child_id in ids:
        result['attempted'] += 1
        child = dict(child_id=child_id)
        try:
            reply = subprocess.run(
                [str(settings.python), str(settings.importer), '--instagram-event-id', str(child_id)],
                shell=False, cwd=str(settings.backend_dir), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=settings.timeout, check=False,
            )
            if reply.returncode == 0:
                child['status'] = 'succeeded'
                result['succeeded'] += 1
            else:
                child.update(status='failed', reason=_failure_reason(getattr(reply, 'stderr', None)),
                             returncode=reply.returncode)
                result['failed'] += 1
        except subprocess.TimeoutExpired:
            child.update(status='failed', reason='timeout_commit_status_unknown')
            result['failed'] += 1
        except Exception:
            # Do not echo exception text, subprocess output, paths or environment.
            child.update(status='failed', reason='importer_execution_failed')
            result['failed'] += 1
        result['children'].append(child)
    if result['failed']:
        result['status'] = 'partial_failure'
    return result


_PARENT_ACTIONS = ('created', 'updated', 'unchanged', 'adopted', 'skipped', 'adoption-required', 'migration-conflict')
_CONFLICT_REASONS = {'multiple_parent_imports', 'legacy_child_imports', 'parent_already_exists',
    'unsafe_adoption', 'dependent_rows', 'adoption_target_not_found', 'unexpected_event_owner'}


def _parent_result(raw, post_id, returncode):
    """Reject unknown fields/content; never return raw output, even on failure."""
    if not isinstance(raw, bytes) or len(raw) > 8192:
        return None
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(data, dict) or set(data) - {'status','action','event_id','event_ids','post_id','reason','source_revision'}:
            return None
        status = data.get('status')
        if status == 'succeeded':
            if returncode != 0 or data.get('action') not in {'created','updated','unchanged','adopted'}:
                return None
            outcome = data['action']
            if data.get('post_id') != post_id or type(data.get('post_id')) is not int:
                return None
        elif status == 'skipped':
            if returncode != 0 or data.get('reason') != 'no_activities':
                return None
            outcome = 'skipped'
        elif status in {'adoption-required','migration-conflict'}:
            if returncode != 2 or (status == 'migration-conflict' and data.get('reason') not in _CONFLICT_REASONS):
                return None
            outcome = status
        else:
            return None
        for key in ('event_id','post_id'):
            if key in data and data[key] is not None and (type(data[key]) is not int or data[key] <= 0):
                return None
        if 'event_ids' in data and (not isinstance(data['event_ids'], list) or
                any(type(i) is not int or i <= 0 for i in data['event_ids'])):
            return None
        if 'source_revision' in data and (not isinstance(data['source_revision'],str) or
                re.fullmatch(r'[0-9a-f]{64}',data['source_revision']) is None):
            return None
        allowed = {'status','event_id','event_ids','post_id','source_revision'}
        if status == 'succeeded':
            allowed.add('action')
        if status in {'migration-conflict','skipped'}:
            allowed.add('reason')
        if set(data) - allowed:
            return None
        return dict(outcome=outcome, **data)
    except (ValueError, TypeError, UnicodeError):
        return None


def sync_posts(post_ids: Iterable[int], settings: SyncSettings | None = None, *, adopt_event_id=None) -> dict:
    """One isolated backend parent upsert per distinct post, never child fan-out.

    Adoption is available only to the explicit CLI, never the automatic stage.
    """
    result = dict(status='completed', attempted=0, succeeded=0, failed=0, parents=[], outcomes={action: 0 for action in _PARENT_ACTIONS})
    ids = list(post_ids)
    if any(type(i) is not int or i <= 0 for i in ids):
        result.update(status='error', error='invalid_post_ids')
        return result
    ids = sorted(set(ids))
    if adopt_event_id is not None and (len(ids) != 1 or type(adopt_event_id) is not int or adopt_event_id <= 0):
        result.update(status='error', error='invalid_adoption_target')
        return result
    if not ids:
        return result
    try:
        settings = settings or SyncSettings.from_env()
        settings.validate()
        env = isolated_import_environment()
        # Nonsecret path to the single shared pure presentation implementation.
        env['INSTAGRAM_PLAYGROUND_DIR'] = str(Path(__file__).resolve().parents[2])
    except Exception:
        result.update(status='error', error='importer_configuration_invalid')
        return result
    for post_id in ids:
        if not hooks.begin('event_master',post_id):
            result['outcomes']['skipped']+=1
            result['parents'].append(dict(parent_id=post_id,status='skipped',reason='state_blocked'))
            continue
        result['attempted'] += 1
        item = dict(parent_id=post_id)
        command = [str(settings.python), str(settings.importer), '--instagram-post-id', str(post_id), '--sync']
        if adopt_event_id is not None:
            command.extend(['--adopt-event-id', str(adopt_event_id)])
        try:
            reply = subprocess.run(command, shell=False, cwd=str(settings.backend_dir), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=settings.timeout, check=False)
            parsed = _parent_result(getattr(reply, 'stdout', None), post_id, reply.returncode)
            if parsed is not None:
                outcome = parsed.pop('outcome')
                result['outcomes'][outcome] += 1
                item.update(parsed)
                if outcome in {'adoption-required', 'migration-conflict'}:
                    result['failed'] += 1
                else:
                    result['succeeded'] += 1
            else:
                item.update(status='failed', reason=('importer_result_invalid' if reply.returncode in (0, 2)
                            else _failure_reason(getattr(reply, 'stderr', None))), returncode=reply.returncode)
                result['failed'] += 1
        except subprocess.TimeoutExpired:
            item.update(status='failed', reason='timeout_commit_status_unknown')
            result['failed'] += 1
        except Exception:
            item.update(status='failed', reason='importer_execution_failed')
            result['failed'] += 1
        hooks.finish('event_master',post_id,item)
        result['parents'].append(item)
    if result['failed']:
        result['status'] = 'partial_failure'
    return result


def run_optional_event_master_sync(committed_parent_ids: set[int]) -> dict:
    enabled = (os.getenv('INSTAGRAM_PIPELINE_SYNC_EVENT_MASTER') or '').strip().lower() in (
        'true', '1', 'yes', 'on',
    )
    result = dict(enabled=enabled, status='disabled' if not enabled else 'completed',
                  attempted=0, succeeded=0, failed=0, parents=[])
    if enabled:
        try:
            if committed_parent_ids:
                result.update(sync_posts(committed_parent_ids))
        except Exception:
            result.update(status='error', error='event_master_sync_selection_or_setup_failed')
    print('Event Master sync: ' + json.dumps(result, sort_keys=True))
    return result
