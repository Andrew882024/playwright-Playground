"""Pure matching and mocked transaction tests; no SQL is executed."""
import copy
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from instagram_event_reconciliation import ReconciliationError, persist_analysis, reconcile_events
from test_instagram_event_extraction import ga, row_six, TZ


def incoming():
    return ga.ai_to_child_event_values(ga.PostAnalysisAI.model_validate(row_six()), TZ)


def child(values, id=11):
    return SimpleNamespace(id=id, instagram_post_id=6, created_at='original',
                           updated_at='previous', is_duplicated=False, **copy.deepcopy(values))


class MemorySession:
    """Transaction double with snapshots, to verify caller write/rollback boundaries."""
    def __init__(self, rows, analyzed=True, fail_add=False, fail_commit=False):
        self.rows = rows
        self.parent = SimpleNamespace(
            id=6, ai_analyzed=analyzed, is_duplicated=False, event_title=None,
            provider_name=None, location=None, event_start_at=None, event_end_at=None
        )
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_add = fail_add
        self.fail_commit = fail_commit
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @contextmanager
    def begin(self):
        old_rows = list(self.rows)
        old_states = [copy.copy(row.__dict__) for row in old_rows]
        parent_state = copy.copy(self.parent.__dict__)
        self.in_transaction = True
        try:
            yield
            if self.fail_commit:
                raise RuntimeError('mock commit failure')
            self.commits += 1
        except Exception:
            self.rows[:] = old_rows
            for row, state in zip(old_rows, old_states):
                row.__dict__.clear(); row.__dict__.update(state)
            self.parent.__dict__.clear(); self.parent.__dict__.update(parent_state)
            self.rollbacks += 1
            raise
        finally:
            self.in_transaction = False

    def execute(self, stmt):
        assert self.in_transaction
        self.statements.append(stmt)
        return SimpleNamespace(scalar_one=lambda: self.parent)

    def scalars(self, stmt):
        assert self.in_transaction
        self.statements.append(stmt)
        return SimpleNamespace(all=lambda: list(self.rows))

    def add(self, row):
        assert self.in_transaction
        if self.fail_add:
            raise RuntimeError('mock insert failure')
        row.id = max([r.id for r in self.rows] + [0]) + 1
        self.rows.append(row)

    def delete(self, row):
        assert self.in_transaction
        self.rows.remove(row)


class ReconciliationTests(unittest.TestCase):
    def test_reordered_reanalysis_preserves_ids(self):
        values = incoming()
        old = [child(values[0], 11), child(values[1], 22)]
        updates, inserts, removals = reconcile_events(old, list(reversed(values)))
        self.assertEqual({v['event_title']: r.id for r, v in updates}, {'Coffee Chats': 11, 'Tech Talk': 22})
        self.assertEqual(inserts, [])
        self.assertEqual(removals, [])
        self.assertEqual(old[0].created_at, 'original')

    def test_case_whitespace_and_utc_equivalence(self):
        values = incoming()[:1]
        old = child(values[0])
        values[0]['event_title'] = '  COFFEE   chats '
        values[0]['event_start_at'] = values[0]['event_start_at'].astimezone(timezone.utc)
        updates, _, _ = reconcile_events([old], values)
        self.assertIs(updates[0][0], old)

    def test_single_field_corrections_preserve_id(self):
        for field, value in [('event_title', 'Coffee with ACM'),
                             ('event_start_at', incoming()[0]['event_start_at'] + timedelta(hours=1))]:
            values = incoming()[:1]
            old = child(values[0], 42)
            values[0][field] = value
            updates, inserts, removals = reconcile_events([old], values)
            self.assertEqual(updates[0][0].id, 42)
            self.assertEqual(inserts, [])
            self.assertEqual(removals, [])

    def test_add_remove_and_successful_zero(self):
        values = incoming()
        old = child(values[0])
        updates, inserts, removals = reconcile_events([old], values)
        self.assertEqual(len(updates), 1)
        self.assertEqual(inserts, [values[1]])
        self.assertEqual(removals, [])
        updates, inserts, removals = reconcile_events([old, child(values[1], 22)], values[:1])
        self.assertEqual([r.id for r in removals], [22])
        self.assertEqual(reconcile_events([old], []), ([], [], [old]))
        self.assertEqual(reconcile_events([], values), ([], values, []))

    def test_changed_title_and_time_rolls_back_instead_of_replacement(self):
        values = incoming()[:1]; old = child(values[0])
        values[0]['event_title'] = 'Different title'
        values[0]['event_start_at'] += timedelta(days=1)
        with self.assertRaises(ReconciliationError):
            reconcile_events([old], values)
        self.assertEqual(old.event_title, 'Coffee Chats')

    def test_ambiguous_title_or_time_does_not_depend_on_order(self):
        values = incoming()
        # The new item could match one old child by title and another by time.
        candidate = dict(values[0], event_start_at=values[1]['event_start_at'])
        for old in ([child(values[0], 11), child(values[1], 22)],
                    [child(values[1], 22), child(values[0], 11)]):
            with self.assertRaises(ReconciliationError):
                reconcile_events(old, [candidate])

    def test_duplicates_and_naive_datetimes_rejected(self):
        values = incoming()[:1]
        with self.assertRaises(ReconciliationError):
            reconcile_events([], values * 2)
        with self.assertRaises(ReconciliationError):
            reconcile_events([child(values[0], 11), child(values[0], 22)], values)
        for field in ('event_start_at', 'event_end_at'):
            invalid = copy.deepcopy(values); invalid[0][field] = datetime(2026, 10, 20, 10)
            with self.assertRaises(ReconciliationError):
                reconcile_events([], invalid)

    def test_atomic_success_updates_parent_and_children_with_lock(self):
        values = incoming()
        old = child(values[0], 42)
        session = MemorySession([old])
        ai = ga.PostAnalysisAI.model_validate(row_six())
        parent_values = ga.ai_to_db_update_values(ai, TZ)
        self.assertTrue(persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', parent_values, values, reanalyze=True))
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.rollbacks, 0)
        self.assertIs(session.rows[0], old)
        self.assertEqual(old.id, 42)
        self.assertEqual(old.created_at, 'original')
        self.assertEqual(len(session.rows), 2)
        self.assertTrue(session.parent.ai_analyzed)
        self.assertEqual(session.parent.event_title, 'Coffee Chats')
        self.assertEqual(session.parent.event_end_at.hour, 12)
        self.assertIsNotNone(session.statements[0]._for_update_arg)
        self.assertIn('instagram_events.instagram_post_id', str(session.statements[1]))

    def test_changed_event_preserves_id_and_invalidates_dedup(self):
        values = incoming()[:1]; old = child(values[0], 42)
        values[0]['location'] = 'New room'
        session = MemorySession([old])
        persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'ai_analyzed': True}, values, reanalyze=True)
        self.assertEqual(old.id, 42)
        self.assertIsNone(old.is_duplicated)
        self.assertEqual(old.location, 'New room')

    def test_successful_empty_clears_children_and_sets_status(self):
        session = MemorySession([child(incoming()[0])])
        ai = ga.PostAnalysisAI.model_validate(dict(events=[], description=row_six()['description']))
        persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', ga.ai_to_db_update_values(ai, TZ), [], reanalyze=True)
        self.assertEqual(session.rows, [])
        self.assertTrue(session.parent.ai_analyzed)
        self.assertFalse(session.parent.is_event)
        self.assertIsNone(session.parent.event_start_at)
        self.assertEqual(session.commits, 1)

    def test_ambiguity_preserves_previous_result_and_status(self):
        for analyzed in (False, True):
            values = incoming()[:1]; old = child(values[0])
            session = MemorySession([old], analyzed=analyzed)
            values[0]['event_title'] = 'New'; values[0]['event_start_at'] += timedelta(days=1)
            with self.assertRaises(ReconciliationError):
                persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'ai_analyzed': True}, values, reanalyze=True)
            self.assertEqual(session.commits, 0)
            self.assertEqual(session.rollbacks, 1)
            self.assertEqual(session.parent.ai_analyzed, analyzed)
            self.assertIs(session.rows[0], old)
            self.assertEqual(old.event_title, 'Coffee Chats')

    def test_write_and_commit_failures_roll_back_both_parent_and_children(self):
        for flag in ('fail_add', 'fail_commit'):
            values = incoming(); old = child(values[0], 42)
            values[0]['location'] = 'Changed'
            session = MemorySession([old], analyzed=False, **{flag: True})
            with self.assertRaises(RuntimeError):
                persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'ai_analyzed': True}, values)
            self.assertEqual(session.commits, 0)
            self.assertEqual(session.rollbacks, 1)
            self.assertFalse(session.parent.ai_analyzed)
            self.assertEqual(len(session.rows), 1)
            self.assertEqual(old.location, 'UCSD')
            self.assertEqual(old.id, 42)

    def test_competing_non_reanalysis_job_skipped_under_lock(self):
        session = MemorySession([], analyzed=True)
        self.assertFalse(persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'ai_analyzed': True}, incoming()))
        self.assertEqual(session.rows, [])
        self.assertEqual(len(session.statements), 1)
        self.assertIsNotNone(session.statements[0]._for_update_arg)

    def test_generated_cover_preserved_for_unchanged_and_time_only_updates(self):
        for time_changed in (False, True):
            values = incoming()[:1]
            old = child(values[0], 42)
            old.generated_image_url = 'https://offline/generated.png'
            old.generated_image_input_hash = 'previous-hash'
            if time_changed:
                values[0]['event_start_at'] += timedelta(minutes=10)
            session = MemorySession([old])
            persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'ai_analyzed': True}, values, reanalyze=True)
            self.assertEqual(old.generated_image_url, 'https://offline/generated.png')
            self.assertEqual(old.generated_image_input_hash, 'previous-hash')
            self.assertEqual(old.id, 42)

    def test_generated_cover_invalidated_when_child_title_changes(self):
        values = incoming()[:1]
        old = child(values[0], 42)
        old.generated_image_url = 'https://offline/generated.png'
        old.generated_image_input_hash = 'previous-hash'
        values[0]['event_title'] = 'New Coffee Title'
        session = MemorySession([old])
        persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'ai_analyzed': True}, values, reanalyze=True)
        self.assertIsNone(old.generated_image_url)
        self.assertIsNone(old.generated_image_input_hash)
        self.assertEqual(old.id, 42)

    def test_changed_parent_source_invalidates_all_matched_child_covers(self):
        values = incoming()
        rows = [child(values[0], 11), child(values[1], 22)]
        for row in rows:
            row.generated_image_url = 'https://offline/generated.png'
            row.generated_image_input_hash = 'previous-hash'
        session = MemorySession(rows)
        session.parent.main_image_url = 'old-source'
        session.parent.own_s3_url_for_main_image = 'original-copy'
        persist_analysis(lambda: session, 'acm.at.ucsd', 'row6', {'main_image_url': 'new-source', 'ai_analyzed': True},
                         values, reanalyze=True)
        self.assertTrue(all(r.generated_image_url is None and r.generated_image_input_hash is None for r in rows))
        self.assertEqual(session.parent.own_s3_url_for_main_image, 'original-copy')
