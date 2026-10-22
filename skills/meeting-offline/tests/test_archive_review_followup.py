"""Synthetic owner isolation, conservative migration, and actionable rename recovery."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_meeting as a
import workspace as w
import test_archive_consistency as baseline


class ArchiveReviewFollowupTests(unittest.TestCase):
    setUp = baseline.ArchiveConsistencyTests.setUp
    tearDown = baseline.ArchiveConsistencyTests.tearDown
    collect = baseline.ArchiveConsistencyTests.collect
    renamed_checkpoint = baseline.ArchiveConsistencyTests.renamed_checkpoint

    def snapshot_archive(self):
        return {str(p.relative_to(self.archive)): p.read_bytes()
                for p in self.archive.rglob('*') if p.is_file()}

    def test_workspace_manifest_is_bound(self):
        result = self.collect()
        manifest = w.read(Path(result['folder']) / '.archive.json')
        self.assertEqual(manifest['workspace_id'], w.workspace_identity(self.root, w.read(self.root / 'state.json')))
        self.assertEqual(manifest['source_id'], '4')
        self.assertEqual(result, self.collect())

    def test_other_workspace_or_source_cannot_overwrite_or_move_same_audio(self):
        first = self.collect()
        manifest = w.read(Path(first['folder']) / '.archive.json')
        copy = self.base / 'copy.m4a'
        copy.write_bytes(Path(first['audio']).read_bytes())
        before = self.snapshot_archive()
        for audio in (copy, Path(first['audio'])):
            for workspace, source in [('workspace-B', '4'), (manifest['workspace_id'], 'other-source'), (None, None)]:
                with self.subTest(audio=audio, workspace=workspace, source=source):
                    with self.assertRaisesRegex(ValueError, '其他工作区或来源'):
                        a.collect(audio, root=self.archive, topic='B会议',
                                  html=first['html'] if workspace else None, png=first['png'] if workspace else None,
                                  _checked_workspace=workspace is not None, workspace_id=workspace, source_id=source)
                    self.assertTrue(audio.is_file())
                    self.assertEqual(before, self.snapshot_archive())

    def test_missing_audio_cannot_recover_with_other_owner(self):
        first = self.collect()
        manifest = w.read(Path(first['folder']) / '.archive.json')
        with self.assertRaisesRegex(ValueError, '其他工作区或来源'):
            a.collect(manifest['move_source_path'], root=self.archive, expected_hash=manifest['audio_sha256'],
                      workspace_id='workspace-B', source_id='4')

    def test_legacy_deliverables_without_owner_are_not_adopted(self):
        first = self.collect()
        path = Path(first['folder']) / '.archive.json'
        manifest = w.read(path)
        manifest.pop('workspace_id'); manifest.pop('source_id')
        w.write(path, manifest)
        before = self.snapshot_archive()
        with self.assertRaisesRegex(ValueError, '旧归档.*新的 --root'):
            self.collect()
        self.assertEqual(before, self.snapshot_archive())

    def test_missing_or_empty_manifest_cannot_erase_ownership(self):
        first = self.collect()
        path = Path(first['folder']) / '.archive.json'
        original = path.read_bytes()
        for bad in (None, b'{}', b'[]'):
            with self.subTest(bad=bad):
                if bad is None:
                    path.unlink()
                else:
                    path.write_bytes(bad)
                before = self.snapshot_archive()
                with self.assertRaisesRegex(ValueError, '归档清单'):
                    self.collect()
                self.assertEqual(before, self.snapshot_archive())
                path.write_bytes(original)

    def test_audio_only_initial_archive_can_bind_and_rename(self):
        source = w.read(self.root / 'state.json')['sources'][0]
        initial = a.collect(source['audio_path'], root=self.archive, topic='初始录音')
        state = w.read(self.root / 'state.json')
        state['sources'][0]['audio_path'] = initial['audio']
        w.write(self.root / 'state.json', state)
        result = self.collect()
        self.assertNotEqual(result['folder'], initial['folder'])
        self.assertEqual(w.read(Path(result['folder']) / '.archive.json')['source_id'], '4')
        self.assertEqual(result, self.collect())

    def test_initial_audio_binding_rename_can_recover(self):
        source = w.read(self.root / 'state.json')['sources'][0]
        initial = a.collect(source['audio_path'], root=self.archive, topic='初始录音')
        state = w.read(self.root / 'state.json')
        state['sources'][0]['audio_path'] = initial['audio']
        w.write(self.root / 'state.json', state)
        old = Path(initial['folder'])
        real_write = w.write
        def fail(path, data):
            if not old.exists() and Path(path).name == '.archive.json':
                raise OSError('synthetic adoption rename failure')
            return real_write(path, data)
        with patch.object(w, 'write', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'adoption rename'):
                self.collect()
        result = self.collect()
        self.assertTrue(Path(result['audio']).is_file())
        self.assertEqual(result, self.collect())

    def test_missing_legacy_audio_only_record_is_not_adopted(self):
        source = w.read(self.root / 'state.json')['sources'][0]
        a.collect(source['audio_path'], root=self.archive)
        before = self.snapshot_archive()
        with self.assertRaisesRegex(ValueError, '旧归档'):
            self.collect()
        self.assertEqual(before, self.snapshot_archive())

    def test_managed_folder_rejects_non_leaf_names(self):
        self.archive.mkdir()
        for name in ('', '.', '..', '/', 'a/b', 'a\\b', '\\', '/absolute', None, 12):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    a.managed_folder(self.archive.resolve(), name)
        self.assertEqual(a.managed_folder(self.archive.resolve(), '正常名称'), self.archive / '正常名称')

    def test_rename_intent_and_both_snapshots_bound_and_checked(self):
        _, _, checkpoint = self.renamed_checkpoint('.archive.json')
        intent = w.read(checkpoint)
        for record in (intent, intent['previous_manifest'], intent['next_manifest']):
            self.assertEqual(record['source_id'], '4')
            self.assertEqual(record['workspace_id'], w.read(self.root / 'state.json')['workspace_id'])
        for target in ('intent', 'previous_manifest', 'next_manifest'):
            changed = w.read(checkpoint)
            record = changed if target == 'intent' else changed[target]
            record['workspace_id'] = 'other-workspace'
            w.write(checkpoint, changed)
            before = self.snapshot_archive()
            with self.assertRaises(ValueError):
                self.collect()
            self.assertEqual(before, self.snapshot_archive())
            w.write(checkpoint, intent)
        self.collect()
        self.assertFalse(checkpoint.exists())

    def test_legacy_rename_intent_is_not_claimed(self):
        _, _, checkpoint = self.renamed_checkpoint('.archive.json')
        intent = w.read(checkpoint)
        intent.pop('workspace_id'); intent.pop('source_id')
        w.write(checkpoint, intent)
        with self.assertRaisesRegex(ValueError, '旧归档'):
            self.collect()
        self.assertTrue(checkpoint.exists())

    def test_rename_path_mismatch_gives_original_path(self):
        _, source, checkpoint = self.renamed_checkpoint('.archive.json')
        intent = w.read(checkpoint)
        intent['source_path'] = '/original/recording.m4a'
        w.write(checkpoint, intent)
        with self.assertRaisesRegex(ValueError, 'audio 路径不同.*original/recording.m4a'):
            self.collect()
        self.assertTrue(checkpoint.exists())

    def test_occupied_rename_target_preserves_both_and_explains_next_step(self):
        _, _, checkpoint = self.renamed_checkpoint('.archive.json')
        intent = w.read(checkpoint)
        old = self.archive / intent['old_folder']
        old.mkdir()
        (old / 'unrelated.txt').write_text('do not overwrite')
        before = self.snapshot_archive()
        with self.assertRaisesRegex(ValueError, '目标已被占用.*根目录之外'):
            self.collect()
        self.assertEqual(before, self.snapshot_archive())


if __name__ == '__main__':
    unittest.main()
