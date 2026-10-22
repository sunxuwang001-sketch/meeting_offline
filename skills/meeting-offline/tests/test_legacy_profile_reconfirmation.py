"""Explicit current selection upgrades only that legacy identity; synthetic HTTP flow."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context
import review
import voiceprint as v
import workspace as w


class LegacyReconfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'),
            'MEETING_OFFLINE_DEVICE_ID': 'legacy-reconfirmation-test', 'MEETING_OFFLINE_SANDBOX': '0'})
        env.start(); self.addCleanup(env.stop)
        self.store = self.base / 'memory.json'
        self.roles = [{'id': 'person-' + key, 'entity_type': 'person', 'name': name,
            'scope': '合成测试', 'role_title': '产品', 'confirmation': '过去已确认',
            'created_at': '2025-01-01', 'aliases': ['旧别名' + key]} for key, name in [('a', '甲'), ('b', '乙')]]
        w.write(self.store, {'schema_version': 1, 'device_id': device_context.device_id(),
            'roles': self.roles, 'terms': [], 'history': []})
        raw = self.base / 'm.json'
        w.write(raw, {'model': 'synthetic', 'duration_seconds': 12, 'segments': [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 12000, 'text': '讨论方案。'}]})
        audio = self.base / 'm.wav'; audio.write_bytes(b'synthetic audio fixture, not real speech')
        initial = self.base / 'initial.json'
        w.write(initial, {'sources': [{'source_id': 'm', 'path': str(raw)}], 'speakers': [
            {'key': 'm-A', 'source_id': 'm', 'source_speaker_id': 0, 'role_name': '甲',
             'identity_group': 'person-a', 'role_confirmed': True,
             'utterances': [{'source_segment_index': 0, 'start_ms': 0, 'end_ms': 12000, 'text': '讨论方案。'}]}],
            'memory_store': str(self.store)})
        self.root = w.initialize(initial, self.base / 'workspace', {'m': audio})
        v.enable(True)

    def revision(self):
        return w.read(self.root / 'state.json')['revision']

    def post_roles(self, mappings):
        server = review.ReviewServer(self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            request = urllib.request.Request(server.origin + '/api/roles', method='POST',
                data=json.dumps({'revision': self.revision(), 'mappings': mappings}).encode(),
                headers={'X-Meeting-Token': server.token, 'Origin': server.origin, 'Content-Type': 'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(json.load(response)['saved'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_http_existing_profile_selection_upgrades_only_selected_and_enrolls(self):
        self.assertEqual(v.confirmed_profiles(w.read(self.root / 'state.json')), {})
        def vectors(root, state, keys):
            self.assertEqual(keys, {'m-A'})
            return {'m-A': [1., 0.]}, {}
        with patch.object(v, 'speaker_vectors', side_effect=vectors):
            self.post_roles([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
        roles = {p['id']: p for p in w.read(self.store)['roles']}
        self.assertIs(roles['person-a']['confirmed_by_user'], True)
        self.assertIn('明确选择已有档案', roles['person-a']['confirmation'])
        self.assertEqual(roles['person-a']['created_at'], '2025-01-01')
        self.assertEqual(roles['person-a']['aliases'], ['旧别名a'])
        self.assertEqual(roles['person-b'], self.roles[1])
        self.assertEqual(v.read_library()['profiles']['person-a']['count'], 1)
        # Same UI payload withdraws this contribution, without any new extraction.
        with patch.object(v, 'speaker_vectors', side_effect=AssertionError('withdrawal must not extract')):
            self.post_roles([{'key': 'm-A', 'keep_unknown': True}])
        self.assertNotIn('person-a', v.read_library()['profiles'])
        self.assertTrue(w.read(self.root / 'state.json')['speakers'][0]['accepted_unknown'])
        self.assertTrue(any(p['id'] == 'person-a' for p in w.read(self.store)['roles']))

    def test_empty_or_unknown_confirmation_does_not_upgrade_legacy_profiles(self):
        before = self.store.read_bytes()
        with patch.object(v, 'speaker_vectors', return_value=({}, {})):
            w.save_roles(self.root, [], self.revision())
            w.save_roles(self.root, [{'key': 'm-A', 'keep_unknown': True}], self.revision())
        self.assertEqual(self.store.read_bytes(), before)

    def test_stale_or_invalid_selection_does_not_upgrade_any_profile(self):
        before = self.store.read_bytes()
        for mappings, revision in [([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}], 0),
                                    ([{'key': 'm-A', 'profile_id': 'person-missing'}], self.revision())]:
            with self.assertRaises(ValueError):
                w.save_roles(self.root, mappings, revision)
        self.assertEqual(self.store.read_bytes(), before)

    def test_legacy_profile_without_created_at_can_be_explicitly_reconfirmed(self):
        memory = w.read(self.store)
        memory['roles'][0].pop('created_at')
        w.write(self.store, memory)
        with patch.object(v, 'speaker_vectors', return_value=({}, {})):
            w.save_roles(self.root, [{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}], self.revision(), via='chat')
        profile = next(p for p in w.read(self.store)['roles'] if p['id'] == 'person-a')
        self.assertTrue(profile['confirmed_by_user'])
        self.assertTrue(profile['created_at'])
        self.assertIn('用户在对话中', profile['confirmation'])

    def test_already_confirmed_profile_does_not_rewrite_memory_on_reselection(self):
        memory = w.read(self.store); memory['roles'][0]['confirmed_by_user'] = True
        w.write(self.store, memory); before = self.store.read_bytes()
        with patch.object(v, 'speaker_vectors', return_value=({}, {})):
            w.save_roles(self.root, [{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}], self.revision())
        self.assertEqual(self.store.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
