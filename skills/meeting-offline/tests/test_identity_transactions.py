"""Synthetic failure/retry tests for workspace + shared identity-store commits."""
import copy
import json
import shutil
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import test_legacy_profile_reconfirmation as fixtures
import memory
import review
import voiceprint as v
import workspace as w


class IdentityTransactionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LegacyReconfirmationTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.store = self.fixture.root, self.fixture.store

    def revision(self):
        return w.read(self.root / 'state.json')['revision']

    def save(self, mappings):
        with patch.object(v, 'speaker_vectors', side_effect=lambda root, state, keys: ({key: [1., 0.] for key in keys}, {})):
            return w.save_roles(self.root, mappings, self.revision())

    def snapshot(self):
        paths = [self.root / n for n in ('state.json', 'analysis.json', 'manifest.json', 'exports/latest.json')]
        paths += [self.store, v.location()]
        return {p: p.read_bytes() if p.exists() else None for p in paths}

    def assert_snapshot(self, before):
        self.assertEqual(before, {p: p.read_bytes() if p.exists() else None for p in before})

    def test_disabled_missing_library_is_not_created_even_if_enabled_mid_save(self):
        library = self.root / 'not-created' / 'voiceprints.json'
        with patch.object(v, 'location', return_value=library), \
                patch.object(v, 'status', side_effect=[{'enabled': False}, {'enabled': True}]), \
                patch.object(v, 'speaker_vectors', side_effect=AssertionError('disabled snapshot')):
            w.save_roles(self.root, [{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}], self.revision())
        self.assertFalse(library.parent.exists())
        self.assertTrue(w.read(self.root / 'state.json')['speakers'][0]['role_confirmed'])

    def test_http_failed_withdrawal_is_not_saved_and_retry_removes_old_sample(self):
        self.save([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}]); before = self.snapshot()
        server = review.ReviewServer(self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            request = urllib.request.Request(server.origin + '/api/roles', method='POST', data=json.dumps({
                'revision': self.revision(), 'mappings': [{'key': 'm-A', 'keep_unknown': True}]}).encode(),
                headers={'X-Meeting-Token': server.token, 'Origin': server.origin, 'Content-Type': 'application/json'})
            with patch.object(v, 'atomic_write', side_effect=OSError('synthetic library failure')):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
                body = json.load(caught.exception)
                self.assertEqual(caught.exception.code, 400)
                self.assertNotIn('saved', body)
                self.assertIn('未保存', body['error'])
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assert_snapshot(before)
        with patch.object(v, 'speaker_vectors', side_effect=AssertionError('withdrawal does not extract')):
            w.save_roles(self.root, [{'key': 'm-A', 'keep_unknown': True}], self.revision())
            w.save_roles(self.root, [{'key': 'm-A', 'keep_unknown': True}], self.revision())
        self.assertNotIn('person-a', v.read_library()['profiles'])
        self.assertTrue(w.read(self.root / 'state.json')['speakers'][0]['accepted_unknown'])

    def test_export_failure_restores_workspace_memory_and_voice_then_retry_is_idempotent(self):
        self.save([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}]); before = self.snapshot()
        mapping = [{'key': 'm-A', 'name': '新合成人物', 'entity_type': 'person', 'scope': '合成测试'}]
        with patch.object(w, 'export', side_effect=OSError('synthetic export failure')):
            with self.assertRaises(OSError): self.save(mapping)
        self.assert_snapshot(before)
        self.save(mapping); self.save(mapping)
        pid = w.read(self.root / 'state.json')['speakers'][0]['identity_group']
        self.assertNotIn('person-a', v.read_library()['profiles'])
        self.assertEqual(v.read_library()['profiles'][pid]['count'], 1)

    def test_failed_new_identity_does_not_create_a_voice_library_or_memory_record(self):
        before = self.snapshot()
        with patch.object(w, 'export', side_effect=OSError('synthetic export failure')):
            with self.assertRaises(OSError):
                self.save([{'key': 'm-A', 'name': '未保存人物', 'entity_type': 'person', 'scope': '测试'}])
        self.assert_snapshot(before)

    def test_partial_memory_failure_rolls_back_and_retry_succeeds(self):
        before = self.snapshot(); writer = memory.atomic_write; writes = []
        def fail_second(path, data):
            if Path(path) == self.store:
                writes.append(path)
                if len(writes) == 2: raise OSError('synthetic second memory write failure')
            return writer(path, data)
        mapping = [{'key': 'm-A', 'name': '新合成人物', 'entity_type': 'person', 'scope': '测试'}]
        with patch.object(memory, 'atomic_write', side_effect=fail_second):
            with self.assertRaises(OSError): self.save(mapping)
        self.assert_snapshot(before)
        self.save(mapping)
        self.assertEqual(len([p for p in w.read(self.store)['roles'] if p['name'] == '新合成人物']), 1)

    def test_optional_extraction_failure_still_clears_obsolete_contribution(self):
        self.save([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
        with patch.object(v, 'speaker_vectors', side_effect=RuntimeError('model unavailable')):
            w.save_roles(self.root, [{'key': 'm-A', 'profile_id': 'person-b'}], self.revision())
        state = w.read(self.root / 'state.json')
        self.assertEqual(state['speakers'][0]['identity_group'], 'person-b')
        self.assertIn('model unavailable', state['voiceprint_enrollment_warning'])
        self.assertNotIn('person-a', v.read_library()['profiles'])

    def test_rollback_holds_shared_locks_and_does_not_erase_concurrent_other_updates(self):
        self.save([{'key': 'm-A', 'profile_id': 'person-a', 'confirm_identity': True}])
        memory.upsert_confirmed(self.store, 'role', {**self.fixture.roles[1], 'confirmation': 'explicit synthetic confirmation'})
        other = copy.deepcopy(w.read(self.root / 'state.json'))
        other['speakers'][0].update(identity_group='person-b', role_confirmed=True)
        started = [threading.Event(), threading.Event()]; finished = [threading.Event(), threading.Event()]
        failures = []; threads = []
        def memory_writer():
            started[0].set()
            try: memory.upsert_confirmed(self.store, 'term', {'id': 'other-meeting-term', 'canonical': '并发词', 'scope': '测试', 'confirmation': '明确确认'})
            except Exception as error: failures.append(error)
            finally: finished[0].set()
        def voice_writer():
            started[1].set()
            try: v.enroll_confirmed(self.root.parent / 'other-meeting', other, ['m-A'])
            except Exception as error: failures.append(error)
            finally: finished[1].set()
        def fail_export(root):
            for target in (memory_writer, voice_writer):
                thread = threading.Thread(target=target); thread.start(); threads.append(thread)
            self.assertTrue(all(e.wait(2) for e in started))
            self.assertFalse(any(e.wait(.05) for e in finished), 'shared writers bypassed transaction locks')
            raise OSError('synthetic export failure')
        with patch.object(v, 'speaker_vectors', side_effect=lambda root, state, keys: ({k: [1., 0.] for k in keys}, {})):
            try:
                with patch.object(w, 'export', side_effect=fail_export), self.assertRaises(OSError):
                    w.save_roles(self.root, [{'key': 'm-A', 'name': '失败新人物', 'entity_type': 'person', 'scope': '测试'}], self.revision())
            finally:
                for thread in threads: thread.join(5)
        self.assertTrue(all(e.is_set() for e in finished)); self.assertEqual(failures, [])
        self.assertEqual(w.read(self.root / 'state.json')['speakers'][0]['identity_group'], 'person-a')
        self.assertTrue(any(t['id'] == 'other-meeting-term' for t in w.read(self.store)['terms']))
        self.assertEqual(set(v.read_library()['profiles']), {'person-a', 'person-b'})
        self.assertFalse(any(p['name'] == '失败新人物' for p in w.read(self.store)['roles']))

    def test_concurrent_matching_cannot_persist_an_uncommitted_identity(self):
        other_root = self.root.parent / 'other-workspace'
        shutil.copytree(self.root, other_root)
        other_state = w.read(other_root / 'state.json')
        other_state['workspace_id'] = 'synthetic-other-workspace'
        other_state['speakers'][0].update(role_confirmed=False, identity_group=None, accepted_unknown=False)
        w.write(other_root / 'state.json', other_state)
        before = self.snapshot()
        started, finished = threading.Event(), threading.Event()
        results, failures, threads = [], [], []
        export = w.export
        def matcher():
            started.set()
            try: results.append(v.match_workspace(other_root))
            except Exception as error: failures.append(error)
            finally: finished.set()
        def fail_original_export(root):
            if Path(root) != self.root:
                return export(root)
            thread = threading.Thread(target=matcher); thread.start(); threads.append(thread)
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(.1), 'matcher read uncommitted identity stores')
            raise OSError('synthetic original export failure')
        with patch.object(v, 'speaker_vectors', side_effect=lambda root, state, keys: ({k: [1., 0.] for k in keys}, {})):
            try:
                with patch.object(w, 'export', side_effect=fail_original_export), self.assertRaises(OSError):
                    w.save_roles(self.root, [{'key': 'm-A', 'name': '未保存新人物', 'entity_type': 'person', 'scope': '测试'}], self.revision())
            finally:
                for thread in threads: thread.join(5)
        self.assertTrue(finished.is_set()); self.assertEqual(failures, [])
        self.assertEqual(results[0]['matches'], {})
        self.assertNotEqual(w.read(other_root / 'state.json')['speakers'][0].get('role_source'), 'voice_match')
        self.assert_snapshot(before)


if __name__ == '__main__': unittest.main()
