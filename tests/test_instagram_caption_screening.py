"""Local, unpaid discovery screening. No live dates/images/services are fetched."""
import contextlib
import io
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as Row
from unittest.mock import Mock,patch
from app.parent_pipeline.screening import screen,eligible
from test_instagram_event_extraction import serial
from app.parent_pipeline import integration as flow,service
from app.parent_pipeline.budget import RunBudget
import run_instagram_discovery_pipeline as cli


class CaptionScreeningTests(unittest.TestCase):
    now=datetime(2026,11,4,20,tzinfo=timezone.utc)
    def check(self,caption):return screen(dict(caption=caption),self.now)
    def test_yearless_dates_use_publication_year(self):
        for caption,posted,expected,status in (
            ('Event Nov 20.',datetime(2026,11,2,tzinfo=timezone.utc),'2026-11-20','eligible'),
            ('Event Oct 24.',datetime(2026,10,22,tzinfo=timezone.utc),'2026-10-24','skipped_expired'),
            ('Event Oct 20.',datetime(2026,11,2,tzinfo=timezone.utc),'2026-10-20','skipped_expired'),
        ):
            with self.subTest(caption=caption):
                result=screen(dict(caption=caption,posted_time=posted),self.now)
                self.assertEqual(result['status'],status)
                self.assertEqual(result['dates'],[expected])
                self.assertEqual(result['date_inferences'],[dict(date=expected,year_source='publication_year')])

    def test_explicit_year_takes_priority_over_publication_year(self):
        for caption in ('Event November 20, 2027.','Event 2027-11-20.'):
            result=screen(dict(caption=caption,posted_time=datetime(2026,11,2,tzinfo=timezone.utc)),self.now)
            self.assertEqual(result['dates'],['2027-11-20'])
            self.assertEqual(result['date_inferences'],[dict(date='2027-11-20',year_source='explicit_year')])

    def test_year_boundary_never_rolls_forward_or_uses_current_year(self):
        result=screen(dict(caption='Event January 5.',posted_time=datetime(2026,12,30,tzinfo=timezone.utc)),
                      datetime(2027,1,1,20,tzinfo=timezone.utc))
        self.assertEqual(result['dates'],['2026-01-05'])
        self.assertEqual(result['status'],'skipped_expired')
        # Both publication year and today's comparison use LA, not the UTC calendar.
        result=screen(dict(caption='Event December 31.',posted_time=datetime(2027,1,1,2,tzinfo=timezone.utc)),
                      datetime(2027,1,1,3,tzinfo=timezone.utc))
        self.assertEqual(result['publication_year'],2026)
        self.assertEqual(result['dates'],['2026-12-31'])
        self.assertEqual(result['reason'],'event_today_time_uncertain')

    def test_publication_epoch_fallback_and_missing_timestamp(self):
        posted=datetime(2026,11,2,tzinfo=timezone.utc)
        for fields in (dict(posted_unix_seconds=int(posted.timestamp())),dict(posted_time=posted.replace(tzinfo=None))):
            result=screen(dict(caption='Event Nov 20.',**fields),self.now)
            self.assertEqual(result['dates'],['2026-11-20'])
        for fields in ({},dict(posted_unix_seconds=True),dict(posted_unix_seconds=float('nan'))):
            self.assertEqual(screen(dict(caption='Event Nov 20.',**fields),self.now)['reason'],'event_year_uncertain')

    def test_inference_preserves_ambiguity_guards_and_handles_multiple_dates(self):
        fields=dict(posted_time=datetime(2026,11,2,tzinfo=timezone.utc))
        for caption in ('Event November.','Event 11/12.','Event next week.','Event February 30.',
                        'Event Friday November 21.'):
            self.assertEqual(screen(dict(caption=caption,**fields),self.now)['status'],'review_required')
        result=screen(dict(caption='Workshops November 2–7; talk November 20, 2027.',**fields),self.now)
        self.assertEqual(result['status'],'eligible')
        self.assertEqual(result['dates'],['2026-11-02','2026-11-07','2027-11-20'])
        self.assertEqual([i['year_source'] for i in result['date_inferences']],
                         ['publication_year','publication_year','explicit_year'])

    def test_preflight_review_counter_matches_screening_without_processing(self):
        selected=[dict(parent_id=i,eligible=status=='eligible',reason='screened',screening=dict(status=status))
                  for i,status in enumerate(('review_required','eligible','skipped_expired','review_required'),1)]
        with patch.object(flow,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(flow,'candidates',return_value=selected),patch.object(flow,'run_parent') as run, \
             patch('app.instagram_scraper.sync_ins.main') as scrape:
            result=flow.execute(profile='dvcatucsd')
        self.assertEqual(result['review_required'],2)
        self.assertEqual(result['processed'],0)
        scrape.assert_not_called();run.assert_not_called()

    def test_candidate_selection_uses_stored_publication_timestamp(self):
        import Blueprint_db
        from unittest.mock import MagicMock
        parent=Row(id=457,profile_username='dvcatucsd',caption='Workshop October 24.',post_title='',
                   posted_time=datetime(2026,10,22,tzinfo=timezone.utc),
                   additional_image_urls={'largest':['https://cdninstagram.com/a.jpg']})
        factory=MagicMock()
        factory.return_value.__enter__.return_value.scalars.return_value.all.return_value=[parent]
        with patch.object(Blueprint_db,'SessionLocal',factory), \
             patch('app.parent_pipeline.attempt_safety.review_required',return_value=False), \
             patch('app.parent_pipeline.screening.eligible',side_effect=lambda p,ids:eligible(p,ids,self.now)):
            result=flow.candidates(['dvcatucsd'],datetime(2026,10,1,tzinfo=timezone.utc))
        self.assertFalse(result[0]['eligible'])
        self.assertEqual(result[0]['screening']['dates'],['2026-10-24'])
        self.assertEqual(result[0]['screening']['status'],'skipped_expired')
    def test_explicit_past_future_and_today(self):
        self.assertEqual(self.check('Rock climbing social on October 24, 2026 at noon.')['status'],'skipped_expired')
        self.assertEqual(self.check('NITRO event: November 20, 2026, 6:30–10 PM at The Loft.')['status'],'eligible')
        self.assertEqual(self.check('Workshop on November 4, 2026.')['status'],'review_required')
    def test_relative_uncertain_missing_invalid_and_numeric_dates(self):
        for caption in ('Workshop this Saturday, October 24.', 'Event on November 20.',
                        'Workshop next Friday.', 'Join us soon!', 'Workshop 11/12/2026.',
                        'Workshop February 30, 2026.','Event November 30–December 2, 2026.'):
            with self.subTest(caption=caption):self.assertEqual(self.check(caption)['status'],'review_required')
    def test_multiple_sessions_and_ranges(self):
        self.assertEqual(self.check('Workshops November 2, 2026 and November 7, 2026.')['status'],'eligible')
        self.assertEqual(self.check('Workshop November 2, 2026; talk November 7, 2026.')['status'],'eligible')
        self.assertEqual(self.check('Workshop October 20, 2026; talk October 24, 2026.')['status'],'skipped_expired')
        self.assertEqual(self.check('Event November 2–7, 2026.')['status'],'eligible')
        self.assertEqual(self.check('Event November 7, 2026 and November 9.')['status'],'review_required')
    def test_weekday_conflict_requires_review(self):
        self.assertEqual(self.check('Event Friday, November 21, 2026.')['reason'],'weekday_date_conflict')
        self.assertEqual(self.check('Event Friday, November 20, 2026.')['status'],'eligible')

    def test_non_event_and_mixed_context_review(self):
        for caption in ('Apply for grants by November 20, 2026. Event funding available.',
                        'Event recap: November 20, 2026.',
                        'Voucher promotion until November 20, 2026.',
                        'Join us at the workshop November 20, 2026. Application deadline November 10, 2026.'):
            self.assertEqual(self.check(caption)['status'],'review_required')
    def test_override_only_for_review_not_expiry_or_uncertain_paid_intent(self):
        parent=Row(id=457,caption='Workshop next Friday.',additional_image_urls={'largest':['https://cdninstagram.com/a.jpg']})
        with patch('app.parent_pipeline.attempt_safety.review_required',return_value=False):
            self.assertEqual(eligible(parent,[457],self.now)['reason'],'explicit_manual_review_override')
            parent.caption='Workshop October 24, 2026.'
            self.assertEqual(eligible(parent,[457],self.now)['status'],'skipped_expired')
        with patch('app.parent_pipeline.attempt_safety.review_required',return_value=True):
            self.assertEqual(eligible(parent,[457],self.now)['reason'],'previous_analysis_outcome_requires_review')
    def test_override_requires_independent_confirmation(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (['--override-review-parent-id','457'],['--confirm-review-override']):
                with self.assertRaises(SystemExit):cli.main(args)
        with self.assertRaises(ValueError):flow.execute(override_ids=[457],confirm_override=False)
    def test_screening_recheck_blocks_before_budget_and_gemini(self):
        parent=Row(ai_analyzed=False)
        budget=RunBudget(1,1)
        with patch.object(service,'parent_lock',return_value=contextlib.nullcontext(True)), \
             patch.object(service,'read_parent',return_value=(parent,[])),patch.object(serial,'main') as analyze:
            result=service.run(457,budget=budget,pending_only=True,
                eligibility_guard=lambda p:dict(status='skipped_expired',reason='explicit_event_dates_expired'))
        self.assertEqual(result['status'],'skipped');self.assertEqual(budget.gemini_attempts,0);analyze.assert_not_called()
    def test_after_scrape_screening_reports_skips_and_reviews_without_paid_processing(self):
        selected=[dict(parent_id=457,eligible=False,reason='explicit_event_dates_expired',screening=dict(status='skipped_expired')),
                  dict(parent_id=458,eligible=False,reason='event_year_uncertain',screening=dict(status='review_required'))]
        with patch.object(flow,'configured_profiles',return_value=['dvcatucsd']), \
             patch.object(flow,'advisory_lock',return_value=contextlib.nullcontext(True)), \
             patch('app.instagram_scraper.sync_ins.main',return_value=dict(profiles=[dict(inserted_parent_ids=[457,458])])), \
             patch.object(flow,'candidates',return_value=selected),patch.object(flow,'run_parent') as run:
            result=flow.execute(profile='dvcatucsd',run=True)
        self.assertEqual(result['skipped'],1);self.assertEqual(result['review_required'],1)
        self.assertEqual(result['gemini_attempts'],0);run.assert_not_called()


class RelativeCaptionScreeningTests(unittest.TestCase):
    def screen(self,caption,posted,now):
        return screen(dict(caption=caption,posted_time=posted),now)

    def test_today_and_tomorrow_are_anchored_to_publication(self):
        posted=datetime(2026,11,4,20,tzinfo=timezone.utc)
        for phrase,expected,status in (('today','2026-11-04','review_required'),
                                       ('tomorrow','2026-11-05','eligible')):
            result=self.screen('Join us for a workshop '+phrase,posted,posted)
            self.assertEqual(result['dates'],[expected])
            self.assertEqual(result['status'],status)
            self.assertEqual(result['date_inferences'],[dict(date=expected,
                year_source='publication_relative_date',relative_phrase=phrase)])
        # Reading an old announcement must never move its event to tomorrow now.
        result=self.screen('Workshop tomorrow',posted,datetime(2026,11,8,20,tzinfo=timezone.utc))
        self.assertEqual(result['dates'],['2026-11-05'])
        self.assertEqual(result['status'],'skipped_expired')

    def test_this_weekday_retains_past_and_upcoming_interpretations(self):
        for day,expected,status in ((4,['2026-11-06'],'eligible'),
                                    (7,['2026-11-06','2026-11-13'],'review_required')):
            posted=datetime(2026,11,day,20,tzinfo=timezone.utc)
            result=self.screen('Workshop this Friday',posted,posted)
            self.assertEqual(result['dates'],expected);self.assertEqual(result['status'],status)

    def test_next_weekday_only_when_unambiguous(self):
        for day in (6,7,8):
            posted=datetime(2026,11,day,20,tzinfo=timezone.utc)
            result=self.screen('Workshop next Friday',posted,posted)
            self.assertEqual(result['dates'],['2026-11-13']);self.assertEqual(result['status'],'eligible')
        posted=datetime(2026,11,4,20,tzinfo=timezone.utc)
        result=self.screen('Workshop next Friday',posted,posted)
        self.assertEqual(result['status'],'review_required')
        self.assertEqual(result['reason'],'ambiguous_next_weekday')

    def test_week_and_year_boundaries(self):
        posted=datetime(2026,11,8,20,tzinfo=timezone.utc)  # Sunday
        self.assertEqual(self.screen('Workshop next Monday',posted,posted)['dates'],['2026-11-09'])
        self.assertEqual(self.screen('Workshop this Monday',posted,posted)['status'],'review_required')
        posted=datetime(2026,12,31,20,tzinfo=timezone.utc)
        result=self.screen('Workshop tomorrow',posted,posted)
        self.assertEqual(result['dates'],['2027-01-01']);self.assertEqual(result['status'],'eligible')
        # Crossing the year is permitted only by the publication-relative rule.
        self.assertEqual(result['publication_year'],2026)

    def test_timezone_dst_epoch_and_local_current_day(self):
        posted=datetime(2027,1,1,2,tzinfo=timezone.utc)  # Dec 31 in LA
        result=self.screen('Workshop tomorrow',posted,datetime(2027,1,1,3,tzinfo=timezone.utc))
        self.assertEqual(result['publication_date'],'2026-12-31')
        self.assertEqual(result['dates'],['2027-01-01']);self.assertEqual(result['status'],'eligible')
        posted=datetime(2026,11,1,6,tzinfo=timezone.utc)  # Oct 31 before DST ends
        result=screen(dict(caption='Workshop tomorrow',posted_unix_seconds=int(posted.timestamp())),
                      datetime(2026,11,1,20,tzinfo=timezone.utc))
        self.assertEqual(result['dates'],['2026-11-01'])
        self.assertEqual(result['reason'],'event_today_time_uncertain')

    def test_ambiguous_relative_phrases_remain_review(self):
        posted=datetime(2026,11,4,20,tzinfo=timezone.utc)
        for phrase in ('this weekend','next week','last Friday','Friday','this coming Friday',
                       'maybe tomorrow','every Friday','not tomorrow','tomorrow or this Friday',
                       'tomorrow, postponed','sometime tomorrow','in two weeks'):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.screen('Workshop '+phrase,posted,posted)['status'],'review_required')
        self.assertEqual(screen(dict(caption='Workshop tomorrow'),posted)['reason'],'relative_publication_date_missing')

    def test_recaps_and_non_events_never_auto_qualify(self):
        posted=datetime(2026,11,4,20,tzinfo=timezone.utc)
        for caption in ('Workshop recap today','We hosted a workshop today','Workshop highlights from today',
                        'Application deadline tomorrow; join us','Promotion tomorrow','Sunny weather tomorrow'):
            self.assertEqual(self.screen(caption,posted,posted)['status'],'review_required')
        self.assertEqual(self.screen('Workshop yesterday',posted,posted)['status'],'skipped_expired')

    def test_explicit_dates_must_agree_with_relative_dates(self):
        posted=datetime(2026,11,4,20,tzinfo=timezone.utc)
        result=self.screen('Workshop this Friday, November 6, 2026',posted,posted)
        self.assertEqual(result['dates'],['2026-11-06']);self.assertEqual(result['status'],'eligible')
        result=self.screen('Workshop tomorrow, November 20, 2026',posted,posted)
        self.assertEqual(result['reason'],'relative_absolute_date_conflict')
        self.assertEqual(result['status'],'review_required')
        result=self.screen('Workshop tomorrow; talk this Friday',posted,posted)
        self.assertEqual(result['dates'],['2026-11-05','2026-11-06'])
        self.assertEqual(result['status'],'eligible')

    def test_expired_relative_date_cannot_be_overridden(self):
        parent=Row(id=457,caption='Workshop tomorrow',posted_time=datetime(2026,11,2,20,tzinfo=timezone.utc),
                   additional_image_urls={'largest':['https://cdninstagram.com/a.jpg']})
        with patch('app.parent_pipeline.attempt_safety.review_required',return_value=False):
            result=eligible(parent,[457],datetime(2026,11,4,20,tzinfo=timezone.utc))
        self.assertEqual(result['status'],'skipped_expired')

    def test_relative_screening_blocks_before_paid_budgets(self):
        parent=Row(id=457,ai_analyzed=False,caption='Workshop tomorrow',
                   posted_time=datetime(2026,11,2,20,tzinfo=timezone.utc),
                   additional_image_urls={'largest':['https://cdninstagram.com/a.jpg']})
        budget=RunBudget(1,1)
        with patch.object(service,'parent_lock',return_value=contextlib.nullcontext(True)), \
             patch.object(service,'read_parent',return_value=(parent,[])), \
             patch('app.parent_pipeline.attempt_safety.review_required',return_value=False), \
             patch.object(serial,'main') as analyze:
            result=service.run(457,budget=budget,pending_only=True,
                eligibility_guard=lambda p:eligible(p,now=datetime(2026,11,4,20,tzinfo=timezone.utc)))
        self.assertEqual(result['status'],'skipped')
        self.assertEqual(budget.gemini_attempts,0);self.assertEqual(budget.cover_attempts,0)
        analyze.assert_not_called()
