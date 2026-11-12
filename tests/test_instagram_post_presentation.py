"""Offline deterministic aggregate presentation tests."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row

from app.instagram_post_presentation import build_post_presentation


def parent(**overrides):
    data = dict(id=6, collection_title=None, provider_name=None, profile_username='acm.at.ucsd',
                post_shortcode='example', ai_analyzed=True, main_image_url='https://example.cdninstagram.com/poster.jpg',
                own_s3_url_for_main_image='', generated_image_url=None, generated_image_input_hash=None)
    return Row(**(data | overrides))


def children():
    start = datetime(2026, 10, 20, 17, tzinfo=timezone.utc)
    return [Row(id=1, instagram_post_id=6, event_title='Coffee Chats', provider_name='ACM',
                event_description='Meet & chat', location='Room A', event_start_at=start,
                event_end_at=start + timedelta(hours=2), duration_in_minutes=120,
                generated_image_url='legacy-child-cover'),
            Row(id=2, instagram_post_id=6, event_title='Tech Talk', provider_name='ACM',
                event_description='Learn <script>bad</script>', location='Room B',
                event_start_at=start+timedelta(hours=7), event_end_at=start+timedelta(hours=8,minutes=30),
                duration_in_minutes=90, generated_image_url=None)]


class PresentationTests(unittest.TestCase):
    def test_multiple_sessions_are_not_a_continuous_duration(self):
        result = build_post_presentation(parent(), children())
        self.assertEqual(result['title'], 'ACM — 2026-10-20')
        self.assertEqual(result['location_summary'], 'Multiple Locations')
        self.assertTrue(result['multi_session'])
        self.assertIsNone(result['duration_in_minutes'])
        self.assertEqual(result['earliest_start'], children()[0].event_start_at)
        for value in ('Coffee Chats', 'Tech Talk', '2026-10-20 10:00 PDT',
                      '2026-10-20 12:00 PDT', '2026-10-20 17:00 PDT', '2026-10-20 18:30 PDT',
                      'Room A', 'Room B'):
            self.assertIn(value, result['description'])
        self.assertNotIn('<script>', result['description'])
        self.assertIn('&lt;script&gt;', result['description'])

    def test_collection_title_and_single_child_rules(self):
        p = parent(collection_title='ACM x Atlassian')
        self.assertEqual(build_post_presentation(p, children())['title'], 'ACM x Atlassian')
        one = build_post_presentation(p, children()[:1])
        self.assertEqual(one['title'], 'Coffee Chats')
        self.assertEqual(one['duration_in_minutes'], 120)
        self.assertFalse(one['multi_session'])

    def test_input_order_and_child_cover_do_not_affect_fingerprint(self):
        rows = children()
        expected = build_post_presentation(parent(), rows)['fingerprint']
        rows[0].generated_image_url = 'different-legacy-cover'
        self.assertEqual(build_post_presentation(parent(), rows[::-1])['fingerprint'], expected)
        rows[1].location = 'Room C'
        self.assertNotEqual(build_post_presentation(parent(), rows)['fingerprint'], expected)

    def test_zero_children_unknowns_and_partial_schedule(self):
        empty = build_post_presentation(parent(), [])
        self.assertEqual(empty['activities'], [])
        self.assertIsNone(empty['earliest_start'])
        rows = children()
        rows[0].event_start_at = rows[0].event_end_at = rows[0].location = rows[0].provider_name = None
        result = build_post_presentation(parent(), rows)
        self.assertTrue(result['has_unknown_start'])
        self.assertIsNone(result['location_summary'])
        self.assertIn('Time not provided', result['description'])
        self.assertIn('Location not provided', result['description'])
        self.assertEqual(result['activities'][0]['id'], 2)

    def test_dates_locations_providers_and_dst(self):
        rows = children()
        rows[1].event_start_at = datetime(2026, 11, 2, 18, tzinfo=timezone.utc)
        rows[1].event_end_at = None
        rows[1].provider_name = 'Other provider'
        result = build_post_presentation(parent(), rows)
        self.assertEqual(result['provider'], 'ACM / Other provider')
        self.assertIn('2026-11-02 10:00 PST', result['description'])
        self.assertIn('Oct 20–Nov 2, 2026', result['title'])

    def test_wrong_parent_and_naive_time_fail_closed(self):
        rows = children()
        rows[0].instagram_post_id = 7
        with self.assertRaises(ValueError):
            build_post_presentation(parent(), rows)
        rows = children()
        rows[0].event_start_at = datetime(2026, 10, 20, 10)
        with self.assertRaises(ValueError):
            build_post_presentation(parent(), rows)


class AggregateTitleLimitTests(unittest.TestCase):
    def test_five_acm_activities_long_caption_and_post_title(self):
        p = parent(id=1597, provider_name='ACM at UCSD', profile_username='acm.at.ucsd',
                   post_shortcode='DfOJ9LAmrV2', post_title='Long Instagram caption ' * 100,
                   caption='Long Instagram caption ' * 100)
        rows = []
        for i, day in enumerate((2, 3, 4, 5, 7)):
            row = children()[0]
            row.id = i + 1
            row.instagram_post_id = 1597
            row.provider_name = 'ACM at UCSD and a different collaborator ' + str(i)
            row.event_start_at = datetime(2026, 11, day, 18, tzinfo=timezone.utc)
            row.event_end_at = row.event_start_at + timedelta(hours=1)
            rows.append(row)
        result = build_post_presentation(p, rows)
        self.assertEqual(result['title'], 'ACM at UCSD — Nov 2–7, 2026')
        self.assertLessEqual(len(result['title']), 100)
        self.assertEqual(build_post_presentation(p, rows[::-1])['title'], result['title'])
        self.assertEqual(build_post_presentation(p, rows[::-1])['fingerprint'], result['fingerprint'])
        p.post_title = p.caption = 'Different caption ' * 100
        self.assertEqual(build_post_presentation(p, rows)['title'], result['title'])

    def test_oversized_or_caption_like_collection_title_uses_fallback(self):
        for value in ('A' * 101, 'Paragraph one\nParagraph two', 'https://example.com/caption', '<p>Caption</p>'):
            with self.subTest(value=value):
                result = build_post_presentation(parent(collection_title=value), children())
                self.assertEqual(result['title'], 'ACM — 2026-10-20')
        self.assertEqual(build_post_presentation(parent(collection_title='A' * 100), children())['title'], 'A' * 100)

    def test_short_explicit_collection_and_single_child_preserved(self):
        self.assertEqual(build_post_presentation(parent(collection_title='ACM Activity Week'), children())['title'], 'ACM Activity Week')
        rows = children()[:1]
        self.assertEqual(build_post_presentation(parent(), rows)['title'], 'Coffee Chats')
        rows[0].event_title = 'Long child title ' * 30
        self.assertEqual(build_post_presentation(parent(), rows)['title'], 'ACM — 2026-10-20')
        self.assertIn('Long child title', build_post_presentation(parent(), rows)['description'])

    def test_long_identities_unknown_dates_and_year_boundary(self):
        rows = children()
        for row in rows:
            row.provider_name = 'Long organizer ' * 30
            row.event_start_at = row.event_end_at = None
        p = parent(provider_name='Long organization ' * 30, profile_username='acm.at.ucsd')
        self.assertEqual(build_post_presentation(p, rows)['title'], 'acm.at.ucsd')
        p.profile_username = 'Long profile ' * 30
        self.assertEqual(build_post_presentation(p, rows)['title'], 'Activities')
        rows[0].event_start_at = datetime(2026,12,31,18,tzinfo=timezone.utc)
        rows[1].event_start_at = datetime(2027,1,2,18,tzinfo=timezone.utc)
        self.assertEqual(build_post_presentation(p, rows)['title'], 'Activities — Dec 31, 2026–Jan 2, 2027')

class PublicDescriptionTests(unittest.TestCase):
    def test_stored_commentary_omitted_without_mutating_facts_or_fingerprint(self):
        from app.instagram_post_presentation import public_description
        warning = ('Please note the potential date discrepancy, as November 7th falls on a Saturday in 2026. '
                   'Verify with organizers before attending.')
        rows = children()
        rows[1].event_description = 'Join the workshop. ' + warning
        before = vars(rows[1]).copy()
        result = build_post_presentation(parent(post_description=warning), rows)
        self.assertEqual(result['public_context'], '')
        self.assertNotIn('Please note', result['description'])
        self.assertNotIn('Verify with', result['description'])
        self.assertIn('Tech Talk', result['description'])
        self.assertIn('Room B', result['description'])
        self.assertEqual(vars(rows[1]), before)
        self.assertEqual(result['activities'][1]['description'], before['event_description'])
        self.assertEqual(public_description('Bring your laptop. Practice Python together. Extra sentence.'),
                         'Bring your laptop. Practice Python together.')

    def test_long_sentence_omitted_instead_of_cut_mid_sentence(self):
        from app.instagram_post_presentation import public_description
        self.assertEqual(public_description('word ' * 80 + '.'), '')
        for warning in ('Fact-check the date.', 'The schedule is ambiguous.',
                        'Please verify details with the organizers.', 'As an AI, I cannot confirm this.'):
            self.assertEqual(public_description(warning), '')
