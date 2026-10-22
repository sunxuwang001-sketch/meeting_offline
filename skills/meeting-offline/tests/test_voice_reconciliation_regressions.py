"""Synthetic contribution withdrawal tests; no model, audio decoder, or API."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context
import voiceprint as v


class VoiceReconciliationRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': self.tmp.name,
            'MEETING_OFFLINE_DEVICE_ID': 'reconciliation-test', 'MEETING_OFFLINE_SANDBOX': '0'})
        env.start(); self.addCleanup(env.stop)
        self.root = Path(self.tmp.name) / 'meeting'
        self.state = {'device_id': device_context.device_id(), 'sources': [
            {'source_id': 'm', 'audio_sha256': 'synthetic-only'}], 'speakers': [
            {'key': 'm-A', 'source_id': 'm', 'role_confirmed': True, 'identity_group': 'a',
             'utterances': [{'start_ms': 0, 'end_ms': 12000}]}]}
        status = patch.object(v, 'status', return_value={'enabled': True, 'available': False})
        status.start(); self.addCleanup(status.stop)
        known = patch.object(v, 'confirmed_profiles', return_value={'a': {}, 'b': {}})
        known.start(); self.addCleanup(known.stop)

    def enroll(self, root=None, state=None, vector=None):
        state = state or self.state
        with patch.object(v, 'speaker_vectors', side_effect=lambda root, state, keys:
                          ({key: vector or [1., 0.] for key in keys}, {})):
            return v.enroll_confirmed(root or self.root, state, [s['key'] for s in state['speakers']])

    def withdraw(self):
        self.state['speakers'][0].update(identity_group=None, accepted_unknown=True)
        with patch.object(v, 'speaker_vectors', side_effect=AssertionError('must not extract')):
            return v.enroll_confirmed(self.root, self.state, ['m-A'])

    def test_unknown_removes_only_sample_and_cannot_match_future_recording(self):
        self.enroll()
        self.withdraw()
        self.assertEqual({}, v.read_library()['profiles'])
        self.assertEqual({}, v.choose_matches({'next-A': [1., 0.]}, v.read_library()['profiles']))
        before = v.location().read_bytes()
        self.withdraw()
        self.assertEqual(before, v.location().read_bytes())

    def test_other_people_recordings_workspaces_and_memory_are_untouched(self):
        memory = device_context.paths()['memory']
        v.atomic_write(memory, {'sentinel': 'role profiles are not biometric samples'})
        self.enroll()
        self.enroll(root=Path(self.tmp.name) / 'another' / 'meeting', vector=[0., 1.])
        other = copy.deepcopy(self.state)
        other['speakers'][0].update(key='n-A', source_id='n', identity_group='b')
        other['sources'][0]['source_id'] = 'n'
        self.enroll(state=other)
        before = copy.deepcopy(v.read_library()['profiles'])
        mem_before = memory.read_bytes()
        self.withdraw()
        after = v.read_library()['profiles']
        self.assertEqual(before['b'], after['b'])
        self.assertEqual(1, after['a']['count'])
        self.assertEqual([0., 1.], after['a']['vector'])
        self.assertEqual(before['a']['sources'][1:], after['a']['sources'])
        self.assertEqual(mem_before, memory.read_bytes())

    def test_reassignment_failure_removes_old_before_extraction(self):
        for error in (None, RuntimeError('synthetic model failure')):
            with self.subTest(error=error):
                self.state['speakers'][0]['identity_group'] = 'a'
                self.enroll()
                self.state['speakers'][0]['identity_group'] = 'b'
                def unavailable(*args):
                    self.assertNotIn('a', v.read_library()['profiles'])
                    if error:
                        raise error
                    return {}, {'m-A': 'model unavailable'}
                with patch.object(v, 'speaker_vectors', side_effect=unavailable):
                    if error:
                        with self.assertRaises(RuntimeError):
                            v.enroll_confirmed(self.root, self.state, ['m-A'])
                    else:
                        self.assertEqual([], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
                self.assertEqual({}, v.read_library()['profiles'])

    def test_disabled_reconciles_without_reading_memory_or_extracting(self):
        self.enroll()
        self.state['speakers'][0]['identity_group'] = 'b'
        with patch.object(v, 'status', return_value={'enabled': False}), \
             patch.object(v, 'confirmed_profiles', side_effect=AssertionError('no enrollment permission')), \
             patch.object(v, 'speaker_vectors', side_effect=AssertionError('must not extract')):
            self.assertEqual('disabled', v.enroll_confirmed(self.root, self.state, ['m-A'])['reason'])
        self.assertEqual({}, v.read_library()['profiles'])

    def test_disabled_unknown_clears_existing_contribution(self):
        self.enroll()
        with patch.object(v, 'status', return_value={'enabled': False}):
            self.assertEqual('disabled', self.withdraw()['reason'])
        self.assertEqual({}, v.read_library()['profiles'])

    def test_reconciliation_write_failure_prevents_new_extraction(self):
        self.enroll()
        before = v.location().read_bytes()
        self.state['speakers'][0]['identity_group'] = 'b'
        with patch.object(v, 'atomic_write', side_effect=OSError('synthetic disk failure')), \
             patch.object(v, 'speaker_vectors') as extract:
            with self.assertRaises(v.ReconciliationError):
                v.enroll_confirmed(self.root, self.state, ['m-A'])
            extract.assert_not_called()
        self.assertEqual(before, v.location().read_bytes())
        self.enroll()
        self.assertNotIn('a', v.read_library()['profiles'])
        self.assertEqual(1, v.read_library()['profiles']['b']['count'])

    def test_same_mapping_and_unsubmitted_key_leave_contributions_unchanged(self):
        self.enroll()
        before = v.location().read_bytes()
        self.enroll()
        self.assertEqual(before, v.location().read_bytes())
        self.state['speakers'][0].update(identity_group=None, accepted_unknown=True)
        v.enroll_confirmed(self.root, self.state, [])
        self.assertEqual(before, v.location().read_bytes())

    def test_legacy_aggregate_survives_removal_of_new_sample(self):
        data = v.read_library()
        # Older vectors have neither per-sample attribution nor a vector_sum.
        data['profiles']['a'] = {'vector': [0., 1.], 'count': 3, 'sources': []}
        v.atomic_write(v.location(), data)
        self.enroll()
        self.withdraw()
        profile = v.read_library()['profiles']['a']
        self.assertEqual(3, profile['count'])
        self.assertEqual([0., 1.], profile['vector'])
        self.assertEqual([0., 3.], profile['vector_sum'])
        self.assertEqual([], profile['sources'])

    def test_old_sample_format_without_workspace_is_retracted_by_sample_id(self):
        self.enroll()
        data = v.read_library()
        data['profiles']['a']['sources'][0].pop('workspace')
        v.atomic_write(v.location(), data)
        self.withdraw()
        self.assertEqual({}, v.read_library()['profiles'])

    def test_unseparable_legacy_sample_invalidates_center_not_other_metadata(self):
        self.enroll()
        data = v.read_library()
        data['profiles']['a']['sources'][0].pop('vector')
        other = {'sample_id': 'unrelated', 'source_id': 'legacy-recording'}
        data['profiles']['a']['sources'].append(other)
        data['profiles']['a']['count'] = 2
        v.atomic_write(v.location(), data)
        self.withdraw()
        profile = v.read_library()['profiles']['a']
        self.assertEqual([other], profile['sources'])
        self.assertEqual(1, profile['count'])
        self.assertNotIn('vector', profile)
        self.assertNotIn('vector_sum', profile)
        self.assertEqual({}, v.choose_matches({'next-A': [1., 0.]}, {'a': profile}))

    def test_disabled_without_library_does_not_create_biometric_data(self):
        with patch.object(v, 'status', return_value={'enabled': False}):
            self.withdraw()
        self.assertFalse(v.location().exists())

    def test_new_provenance_removes_old_interval_versions(self):
        self.enroll()
        self.state['speakers'][0]['utterances'][0]['end_ms'] = 15000
        self.withdraw()
        self.assertEqual({}, v.read_library()['profiles'])


if __name__ == '__main__':
    unittest.main()
