"""Review follow-up: explicit consent and unavailable-library degradation."""
import json
from copy import deepcopy
import urllib.error
import unittest
from unittest.mock import patch
import test_legacy_profile_reconfirmation as fixtures
import voiceprint as v
import workspace as w


class IdentityReviewFollowupTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LegacyReconfirmationTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root, self.store = self.f.root, self.f.store

    def save(self, mappings):
        return w.save_roles(self.root, mappings, self.f.revision())

    def test_http_resend_confirmed_legacy_card_does_not_upgrade_or_enroll(self):
        before = self.store.read_bytes()
        with patch.object(v, 'speaker_vectors', side_effect=AssertionError('no consent')):
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a'}])
        self.assertEqual(self.store.read_bytes(), before)
        self.assertFalse(v.location().exists())

    def test_http_changed_selection_upgrades_only_selected(self):
        with patch.object(v, 'speaker_vectors', return_value=({'m-A': [1., 0.]}, {})):
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-b'}])
        roles = {x['id']: x for x in w.read(self.store)['roles']}
        self.assertNotIn('confirmed_by_user', roles['person-a'])
        self.assertTrue(roles['person-b']['confirmed_by_user'])
        self.assertEqual(set(v.read_library()['profiles']), {'person-b'})

    def test_unconfirmed_legacy_card_can_be_explicitly_selected(self):
        state = w.read(self.root / 'state.json'); state['speakers'][0]['role_confirmed'] = False
        w.write(self.root / 'state.json', state)
        with patch.object(v, 'speaker_vectors', return_value=({}, {})):
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a'}])
        self.assertTrue(next(x for x in w.read(self.store)['roles'] if x['id'] == 'person-a')['confirmed_by_user'])

    def test_invalid_legacy_profile_returns_http400_not500(self):
        memory = w.read(self.store); memory['roles'][0].pop('name'); w.write(self.store, memory)
        before = self.store.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
        self.assertEqual(error.exception.code, 400)
        self.assertIn('name', json.load(error.exception)['error'])
        self.assertEqual(before, self.store.read_bytes())

    def test_one_explicit_card_cannot_enroll_resent_card_sharing_legacy_profile(self):
        state = w.read(self.root / 'state.json')
        other = deepcopy(state['speakers'][0]); other['key'] = 'm-B'
        state['speakers'].append(other); w.write(self.root / 'state.json', state)
        with patch.object(v, 'speaker_vectors', return_value=({'m-A': [1., 0.]}, {})) as extract:
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True},
                               {'key': 'm-B', 'profile_id': 'person-a'}])
        self.assertEqual(extract.call_args.args[2], {'m-A'})
        sources = v.read_library()['profiles']['person-a']['sources']
        self.assertEqual([x['speaker_key'] for x in sources], ['m-A'])

    def test_explicit_confirmation_can_supply_missing_legacy_confirmation(self):
        memory = w.read(self.store); memory['roles'][0].pop('confirmation')
        w.write(self.store, memory)
        with patch.object(v, 'speaker_vectors', return_value=({}, {})):
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
        profile = next(x for x in w.read(self.store)['roles'] if x['id'] == 'person-a')
        self.assertTrue(profile['confirmed_by_user'])
        self.assertTrue(profile['confirmation'])

    def test_unchanged_chat_card_with_incomplete_legacy_profile_still_saves(self):
        memory = w.read(self.store); memory['roles'][0].pop('confirmation'); w.write(self.store, memory)
        before = self.store.read_bytes()
        with patch.object(v, 'speaker_vectors', side_effect=AssertionError('no consent')):
            self.save([{'key': 'm-A', 'profile_id': 'person-a'}])
        self.assertEqual(self.store.read_bytes(), before)
        self.assertFalse(v.location().exists())

    def test_explicit_card_with_incomplete_legacy_profile_explains_in_chinese(self):
        memory = w.read(self.store); memory['roles'][0].pop('scope'); w.write(self.store, memory)
        with self.assertRaisesRegex(ValueError, '记录不完整'):
            self.save([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])

    def test_malformed_legacy_revision_returns_http400(self):
        for value in ('old', None, True, 0):
            with self.subTest(value=value):
                memory = w.read(self.store); memory['roles'][0]['revision'] = value
                w.write(self.store, memory); before = self.store.read_bytes()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
                self.assertEqual(error.exception.code, 400)
                self.assertIn('revision', json.load(error.exception)['error'])
                self.assertEqual(before, self.store.read_bytes())

    def test_malformed_legacy_role_title_returns_http400(self):
        memory = w.read(self.store); memory['roles'][0]['role_title'] = 123
        w.write(self.store, memory); before = self.store.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.f.post_roles([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
        self.assertEqual(error.exception.code, 400)
        self.assertIn('role_title', json.load(error.exception)['error'])
        self.assertEqual(before, self.store.read_bytes())

    def test_unreadable_libraries_allow_change_with_actionable_warning_and_no_write(self):
        valid = v.read_library()
        variants = ['{broken', json.dumps({**valid, 'model': 'other'}),
                    json.dumps({**valid, 'device_id': 'other-device'})]
        for raw in variants:
            with self.subTest(raw=raw):
                v.location().parent.mkdir(parents=True, exist_ok=True)
                v.location().write_text(raw)
                with patch.object(v, 'speaker_vectors', side_effect=AssertionError('unreadable')):
                    self.f.post_roles([{'key': 'm-A', 'keep_unknown': True}])
                state = w.read(self.root / 'state.json')
                self.assertTrue(state['speakers'][0]['accepted_unknown'])
                self.assertIn('备份', state['voiceprint_enrollment_warning'])
                self.assertEqual(v.location().read_text(), raw)

    def test_reconciliation_receives_only_changed_keys(self):
        with patch.object(v, 'reconcile_samples') as reconcile, patch.object(v, 'speaker_vectors', return_value=({}, {})):
            self.save([{'key': 'm-A', 'profile_id': 'person-a'}])
            self.assertEqual(reconcile.call_args.args[2], set())
            self.save([{'key': 'm-A', 'keep_unknown': True}])
            self.assertEqual(reconcile.call_args.args[2], {'m-A'})

    def test_library_enrollment_write_failure_rejects_and_rolls_back(self):
        before = self.store.read_bytes(); revision = self.f.revision()
        with patch.object(v, 'speaker_vectors', return_value=({'m-A': [1., 0.]}, {})), \
             patch.object(v, 'atomic_write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(v.ReconciliationError, '写权限和磁盘空间'):
                self.save([{'key': 'm-A', 'profile_id': 'person-b'}])
        self.assertEqual(before, self.store.read_bytes())
        self.assertEqual(revision, self.f.revision())
        self.assertFalse(v.location().exists())


if __name__ == '__main__': unittest.main()
