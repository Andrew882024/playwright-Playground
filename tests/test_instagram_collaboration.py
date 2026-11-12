"""Pure synthetic metadata tests; no browser, ORM, environment or importer."""
import copy
import unittest

from app.instagram_collaboration import (
    AcceptedCollaborationEvidence as Evidence, normalize_collaboration as normalize,
    organizer_presentation as present, stable_post_key, compare_post_identity,
)


def post(author='primary', code='CaseSensitiveCode'):
    return dict(code=code, pk='12345', user={'username': author})


class CollaborationTests(unittest.TestCase):
    def test_ordinary_post(self):
        result = normalize(post(), 'primary')
        self.assertEqual(result.primary_author, 'primary')
        self.assertEqual(result.requested_profile, 'primary')
        self.assertEqual(result.collaborators, ())
        self.assertEqual(present(result)['organizer_display'], '@primary')
        self.assertFalse(result.review_required)

    def test_two_account_confirmation_requires_separate_trusted_evidence(self):
        raw = post(); raw['coauthor_producers'] = [{'username': 'partner', 'is_confirmed': True}]
        hinted = normalize(raw, 'primary')
        self.assertEqual(hinted.collaborators[0].status, 'unverified')
        self.assertEqual(present(hinted)['organizer_display'], '@primary')
        confirmed = normalize(raw, 'primary', accepted_evidence=[Evidence(raw['code'], 'primary', 'partner')])
        self.assertEqual(confirmed.collaborators[0].status, 'confirmed')
        self.assertEqual(present(confirmed)['organizer_display'], '@primary & @partner')
        self.assertFalse(confirmed.review_required)

    def test_multiple_collaborators_are_deterministic(self):
        raw = post()
        evidence = [Evidence(raw['code'], 'primary', name) for name in ('zeta', 'alpha')]
        one = normalize(raw, accepted_evidence=evidence)
        two = normalize(raw, accepted_evidence=list(reversed(evidence)))
        self.assertEqual(one, two)
        self.assertEqual(present(one)['organizer_display'], '@primary & @alpha & @zeta')

    def test_pending_invitation_never_confirms_acceptance(self):
        raw = post(); raw['invited_coauthors'] = [{'username': 'partner', 'status': 'accepted'}]
        result = normalize(raw)
        self.assertEqual(result.collaborators[0].status, 'pending')
        self.assertTrue(present(result)['review_required'])
        self.assertEqual(present(result)['organizer_display'], '@primary')

    def test_payload_cannot_smuggle_accepted_evidence(self):
        raw = post(); raw['accepted_evidence'] = [dict(collaborator='partner', primary_author='primary')]
        raw['collaborators'] = [{'username': 'partner', 'confirmed': True}]
        self.assertEqual(normalize(raw).collaborators, ())
        result = normalize(raw, accepted_evidence=[{'post_shortcode': raw['code'], 'collaborator': 'partner'}])
        self.assertEqual(result.collaborators, ())
        self.assertTrue(result.review_required)

    def test_duplicate_accounts_are_case_insensitive_and_primary_is_excluded(self):
        raw = post(); raw['coauthor_producers'] = [{'username': n} for n in ('PARTNER', '@partner', 'primary')]
        result = normalize(raw)
        self.assertEqual(len(result.collaborators), 1)
        self.assertEqual(result.collaborators[0].username, 'partner')

    def test_pending_and_hint_duplicates_retain_pending(self):
        raw = post(); raw['coauthor_producers'] = [{'username': 'partner'}]
        raw['invited_coauthors'] = [{'username': 'PARTNER'}]
        self.assertEqual(normalize(raw).collaborators[0].status, 'pending')

    def test_confirmation_is_bound_to_exact_post_and_primary_author(self):
        for evidence in (Evidence('OtherCode', 'primary', 'partner'),
                         Evidence('CaseSensitiveCode', 'different_author', 'partner')):
            result = normalize(post(), accepted_evidence=[evidence])
            self.assertEqual(result.collaborators, ())
            self.assertIn('accepted_evidence_identity_mismatch', result.review_reasons)

    def test_different_primary_authors_do_not_share_confirmation(self):
        one = normalize(post(), accepted_evidence=[Evidence('CaseSensitiveCode', 'primary', 'partner')])
        two = normalize(post('different_author'), accepted_evidence=[Evidence('CaseSensitiveCode', 'primary', 'partner')])
        self.assertEqual(present(one)['organizer_display'], '@primary & @partner')
        self.assertEqual(present(two)['organizer_display'], '@different_author')
        self.assertEqual(compare_post_identity(one, two), 'identity_conflict')

    def test_same_post_from_two_profiles_preserves_discovery_context(self):
        raw = post(); raw['coauthor_producers'] = [{'username': 'partner'}]
        left = normalize(raw, 'primary'); right = normalize(raw, 'partner')
        self.assertNotEqual(left.requested_profile, right.requested_profile)
        self.assertEqual(stable_post_key(left), stable_post_key(right))
        self.assertEqual(compare_post_identity(left, right), 'same_post_candidate')
        self.assertEqual(present(right)['organizer_display'], '@primary')
        self.assertTrue(right.review_required)

    def test_missing_and_malformed_media_never_use_discovery_profile_as_author(self):
        for raw in (None, [], 'private data', {}, {'user': None}, {'user': {'username': '<script>'}}):
            result = normalize(raw, 'discovered')
            self.assertIsNone(result.primary_author)
            self.assertEqual(present(result)['organizer_display'], '')
            self.assertTrue(result.review_required)

    def test_owner_fallback_and_disagreement(self):
        raw = {'owner': {'username': 'primary'}}
        self.assertEqual(normalize(raw).primary_author, 'primary')
        raw['user'] = None
        self.assertIsNone(normalize(raw).primary_author)
        raw = post(); raw['owner'] = {'username': 'different_author'}
        result = normalize(raw, accepted_evidence=[Evidence(raw['code'], 'primary', 'partner')])
        self.assertIn('author_owner_conflict', result.review_reasons)
        self.assertEqual(result.collaborators, ())

    def test_malformed_collaborators_and_bounds(self):
        for values in (None, {}, 'private data', [None, {}, {'username': 'bad/name'}]):
            raw = post(); raw['coauthor_producers'] = values
            result = normalize(raw)
            self.assertEqual(result.collaborators, ())
            self.assertTrue(result.review_required)
        raw = post(); raw['coauthor_producers'] = [{'username': f'partner{i}'} for i in range(40)]
        result = normalize(raw)
        self.assertEqual(len(result.collaborators), 32)
        self.assertIn('collaborators_truncated', result.review_reasons)

    def test_invalid_requested_profile_and_evidence(self):
        result = normalize(post(), 'bad/name', accepted_evidence={'not': 'trusted'})
        self.assertIsNone(result.requested_profile)
        self.assertIn('invalid_requested_profile', result.review_reasons)
        self.assertIn('invalid_accepted_evidence', result.review_reasons)

    def test_similar_content_never_establishes_post_identity(self):
        raw = post(); raw.update(caption={'text': 'same event'}, image_url='same image', date='same date')
        other = copy.deepcopy(raw); other['code'] = 'DifferentCode'; other['pk'] = '99999'
        self.assertEqual(compare_post_identity(normalize(raw), normalize(other)), 'different_posts')

    def test_identity_conflicts_require_review(self):
        raw = post(); other = post(); other['pk'] = '99999'
        self.assertEqual(compare_post_identity(normalize(raw), normalize(other)), 'identity_conflict')
        other = post(code='DifferentCode')
        self.assertEqual(compare_post_identity(normalize(raw), normalize(other)), 'identity_conflict')

    def test_shortcode_case_and_missing_identity_are_not_guessed(self):
        raw = post(); other = post(code=raw['code'].lower()); other['pk'] = '99999'
        self.assertEqual(compare_post_identity(normalize(raw), normalize(other)), 'different_posts')
        for value in (None, '', 'bad/code', True, 'https://instagram.com/p/a/'):
            raw['code'] = value
            result = normalize(raw)
            self.assertIsNone(stable_post_key(result))
            self.assertEqual(compare_post_identity(result, normalize(post())), 'insufficient_identity')

    def test_normalizer_does_not_mutate_input_or_retain_unrelated_private_fields(self):
        raw = post(); raw.update(caption='PRIVATE_CAPTION', cookies='PRIVATE_COOKIE', url='PRIVATE_URL')
        before = copy.deepcopy(raw); result = normalize(raw)
        self.assertEqual(raw, before)
        self.assertNotIn('PRIVATE', repr(result))

    def test_display_contains_only_validated_plain_account_names(self):
        raw = post(); raw['coauthor_producers'] = [{'username': '<img onerror=x>'}]
        result = normalize(raw, accepted_evidence=[Evidence(raw['code'], 'primary', '<script>')])
        self.assertEqual(present(result)['organizer_display'], '@primary')


if __name__ == '__main__':
    unittest.main()
