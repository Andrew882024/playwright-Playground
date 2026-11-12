"""Offline excerpts from the 457–463 audit; retain all date expressions/cues.

No fixture loads database rows, images, network resources, or real configuration.
"""
import unittest
from datetime import datetime, timezone
from app.parent_pipeline.screening import screen


AUDITED = {
    457: ('2026-10-22T17:56:50+00:00', '''THIS SATURDAY, we’re kicking off our FIRST DVC SOCIAL with a ROCK CLIMBING HANGOUT!
★ THIS SATURDAY, OCTOBER 24
★ MEET @ THE UCSD OUTBACK CLIMBING CENTER
★ 12:00 PM
bring ur friends or come solo! everyone’s welcome!!'''),
    458: ('2026-10-19T17:31:18+00:00', '''GAME NIGHT GBM THIS WEDNESDAY!!
this WEDNESDAY, we’re getting into the roots of DVC, festivals, and how YOU can turn your own ideas into events!
we’re also running a fun little game activity, so pull through to have fun!!
stick around after for open decks, studio hangout, & meeting fellow ravers
✦ Wednesday, October 21
✦ 6:30–8:00 PM
✦ DVC Studio, Old Student Center (2nd floor)'''),
    459: ('2026-10-15T16:45:02+00:00', '''STUDIO HOURS ARE LIVE FOR FALL QUARTER!!
here is our WEEKLY set schedule for our staff studio hours (fall 2026)!! Pull up to the DVC Studio to talk with staff,
ask questions, practice your mixing, or even just to hang out & vibe!!
MONDAY:
Corey ~ 12:30-2:30 PM
Alexia ~ 4-6 PM
TUESDAY:
Nate ~ 11-2 PM
Arushi ~ 2-4 PM
Alex ~ 4-6 PM
WEDNESDAY:
Jeyden ~ 3-4:30 PM
Nate ~ 3-4:30 PM
Alexia ~ 5-6 PM
THURSDAY:
Alex ~ 3:30-4:30 PM
Alexia ~ 4-6 PM'''),
    460: ('2026-10-07T20:13:05+00:00', '''wanna learn how to DJ? come learn with us!!
every other Wednesday, we’re opening up the DVC Studio for hands-on DJ workshops!!
whether you’re completely new or just wanna sharpen your skills, come through and mess around on the decks with us
📍 DVC Studio (2nd floor of the old student center)
📍 Wednesdays • 6:30–8PM
✦ Week 3 ~~ 10/14
✦ Week 5 ~~ 10/28
✦ Week 7 ~~ 11/11
no experience necessary!! just bring yourself :)
see you behind the decks'''),
    461: ('2026-10-04T00:00:11+00:00', '''What’s up UCSD
We are continuing to start the fall quarter strong with our first General Body Meeting of the year THIS TUESDAY!
Learn more about DVC and how to get involved! Meet the community & participate in a fun social activity
Looking forward to seeing you at our studio in the Old Student Center (6:00-7:30 PM)'''),
    462: ('2026-09-30T18:35:20+00:00', '''THE SUN GOD AFTERDARK LINEUP IS HERE!!
Catch:
6:00 ~ DOTKSEND
6:30 ~ NILOC
7:00 ~ FOXER
7:30 ~ JEY
8:00 ~ HYPER
8:30 ~ FEVER
9:00 ~ SZENTE
9:30 ~ DJENIRATE
📍 Sun God Lawn
📅 October 2 (this friday!!!)
⏰ 6–10 PM
Bring your friends & come dance THIS friday night!!'''),
    463: ('2026-09-25T18:53:16+00:00', '''DJ SPOTLIGHT: @dvcatucsd will be providing the tunes tonight! Doors open at 8pm. See you soon!
About DVC:
Founded in 1997, DVC has been a thriving hub for bringing together music enthusiasts that share the same interests and passions.
We provide free DJ trainings for students, organize music events, play at different venues for hiring services,
and more importantly, contribute to building a welcoming and versatile community for everyone interested in the music scene.
🗓️ Friday, September 25th
🕕 8pm - 11pm
📍The Loft
Free for all UCSD students with valid ID, while supplies last.'''),
}


class AuditedCaptionTests(unittest.TestCase):
    now = datetime(2026, 11, 4, 20, tzinfo=timezone.utc)

    def check(self, text, posted='2026-11-02T20:00:00+00:00', now=None):
        return screen(dict(caption=text, posted_time=datetime.fromisoformat(posted)), now or self.now)

    def test_all_seven_audited_patterns(self):
        for parent_id, (posted, caption) in AUDITED.items():
            with self.subTest(parent_id=parent_id):
                result = self.check(caption, posted)
                expected = 'eligible' if parent_id == 460 else 'review_required' if parent_id == 459 else 'skipped_expired'
                self.assertEqual(result['status'], expected, result)
        result = self.check(AUDITED[460][1], AUDITED[460][0])
        self.assertEqual(result['dates'], ['2026-10-14', '2026-10-28', '2026-11-11'])
        self.assertEqual(result['confirmed_dates'], result['dates'])
        self.assertTrue(all(i['year_source'] == 'publication_year' for i in result['date_inferences']))

    def test_461_does_not_invent_a_confirmed_tuesday(self):
        result = self.check(AUDITED[461][1], AUDITED[461][0])
        self.assertEqual(result['dates'], ['2026-09-29', '2026-10-06'])
        self.assertEqual(result['confirmed_dates'], [])
        self.assertEqual(result['reason'], 'all_plausible_event_dates_expired')
        self.assertEqual(result['uncertainties'][0]['reason'], 'ambiguous_this_weekday')
        self.assertEqual(result['publication_date'], '2026-10-03')

    def test_ordinary_or_around_phrases_do_not_qualify_as_date_uncertainty(self):
        for suffix in ('bring friends or come solo', 'stick around after', 'meet new people or old friends'):
            with self.subTest(suffix=suffix):
                result = self.check('Workshop this Friday, November 6. ' + suffix)
                self.assertEqual(result['status'], 'eligible', result)

    def test_date_uncertainty_remains_review_even_when_one_date_is_past(self):
        for text in ('Workshop Friday or Saturday', 'Workshop around November 20',
                     'Workshop maybe tomorrow', 'Workshop November 1 or November 20',
                     'Workshop tomorrow or this Friday', 'Workshop sometime November 20'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text)['status'], 'review_required')

    def test_time_ranges_are_not_calendar_dates(self):
        for clock in ('6:00-7:30 PM', '6:00–7:30 PM', '12:30-2:30 PM', '6:00-17:30', '4-6 PM'):
            with self.subTest(clock=clock):
                result = self.check('Workshop October 24, 2026, ' + clock)
                self.assertEqual(result['status'], 'skipped_expired', result)
                self.assertEqual(result['dates'], ['2026-10-24'])
        self.assertEqual(self.check('Workshop 6:00-7:30 PM')['status'], 'review_required')

    def test_ambiguous_weekdays_need_all_interpretations_expired(self):
        caption = 'Workshop this Tuesday 6:00-7:30 PM'
        posted = '2026-10-04T00:00:11+00:00'
        middle = self.check(caption, posted, datetime(2026,10,4,20,tzinfo=timezone.utc))
        self.assertEqual(middle['status'], 'review_required')
        self.assertEqual(middle['dates'], ['2026-09-29', '2026-10-06'])
        future = self.check('Workshop next Friday', '2026-11-02T20:00:00+00:00')
        self.assertEqual(future['dates'], ['2026-11-06', '2026-11-13'])
        self.assertEqual(future['status'], 'review_required')

    def test_unresolved_or_future_sessions_prevent_premature_expiry(self):
        for caption in ('Workshop this Tuesday; talk next month',
                        'Workshop this Tuesday; talk next year',
                        'Workshop this Tuesday; another session soon',
                        'Workshop this Tuesday; another talk date TBD',
                        'Workshop this Tuesday; another talk November',
                        'Workshop this Tuesday; another talk November 20',
                        'Workshop this Tuesday; another talk 11-20'):
            self.assertEqual(self.check(caption, AUDITED[461][0])['status'], 'review_required')

    def test_recurring_series_validates_all_weekdays_and_cadence(self):
        text = AUDITED[460][1]
        for replacement in ('11/12', '11/18', '11/31', '13/11'):
            result = self.check(text.replace('11/11', replacement), AUDITED[460][0])
            self.assertEqual(result['status'], 'review_required', result)
        undated = 'Workshops every Wednesday at 6:30 PM in the studio'
        self.assertEqual(self.check(undated)['status'], 'review_required')
        self.assertEqual(self.check('Workshop Friday, 11/20/2026')['status'], 'eligible')
        self.assertEqual(self.check('Workshop Thursday, 11/20/2026')['reason'], 'weekday_date_conflict')

    def test_numeric_ambiguity_invalidity_and_explicit_year_priority(self):
        for text in ('Workshop 11/12', 'Workshop 13/11', 'Workshop 2/30', 'Workshop 11/20/26'):
            self.assertEqual(self.check(text)['status'], 'review_required')
        result = self.check('Workshop 11/20/2027')
        self.assertEqual(result['dates'], ['2027-11-20'])
        self.assertEqual(result['date_inferences'][0]['year_source'], 'explicit_year')
        # Another event's clear US-format date does not resolve 10/11.
        self.assertEqual(self.check('Workshop 10/14; talk 10/11')['status'], 'review_required')

    def test_numeric_year_boundary_never_rolls_forward(self):
        result = self.check('Workshop 1/20', '2026-12-30T20:00:00+00:00',
                            datetime(2027,1,1,20,tzinfo=timezone.utc))
        self.assertEqual(result['dates'], ['2026-01-20'])
        self.assertEqual(result['status'], 'skipped_expired')

    def test_lineup_requires_dates_time_and_attendance_venue_cues(self):
        for text in ('New product lineup', 'Lineup November 20',
                     'Lineup November 20, 6 PM', 'Product lineup November 20, 6 PM at the store'):
            self.assertEqual(self.check(text)['status'], 'review_required')
        self.assertEqual(self.check('Lineup: catch live sets November 20, 6 PM 📍 The Loft')['status'], 'eligible')

    def test_foundation_clause_does_not_hide_other_events_or_deadlines(self):
        result = self.check(AUDITED[463][1], AUDITED[463][0])
        self.assertEqual(result['dates'], ['2026-09-25'])
        self.assertEqual(result['ignored_context'], ['organization_founding_year'])
        for addition in (' Application deadline November 20.', ' Event recap.', ' Promotion November 20.'):
            self.assertEqual(self.check(AUDITED[463][1]+addition, AUDITED[463][0])['status'], 'review_required')
        result = self.check(AUDITED[463][1]+' Workshop November 20.', AUDITED[463][0])
        self.assertEqual(result['status'], 'eligible')
        self.assertIn('2026-11-20', result['dates'])

    def test_invalid_early_date_does_not_hide_later_parsed_date(self):
        result = self.check('Workshop February 30; talk November 20')
        self.assertEqual(result['status'], 'review_required')
        self.assertIn('2026-11-20', result['dates'])
        self.assertIn('invalid_event_date', result['reasons'])
