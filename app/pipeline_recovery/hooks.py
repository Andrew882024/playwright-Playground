"""Run-scoped normal-stage hooks. Absent outside automatic pipeline execution."""
from contextvars import ContextVar
current_run = ContextVar('instagram_recovery_run', default=None)


def begin(stage, parent_id):
    run = current_run.get()
    return run.begin_normal(stage, parent_id) if run else True


def finish(stage, parent_id, result):
    run = current_run.get()
    if run:
        run.finish_normal(stage, parent_id, result)
