"""Synthetic archive checkpoint recovery; never opens real recordings or services."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_meeting as a
import workspace as w
import test_workspace
from test_obsidian import fake_png


class ArchiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_workspace.WorkspaceTests()
        self.fixture.setUp()
        self.root = self.fixture.root
        self.archive = self.fixture.base / 'archive'
        with patch.object(w.minutes_html, 'png_for', side_effect=fake_png):
            w.set_analysis(self.root, {'sources': {'4': {'reader': {
                'topic': '合成归档恢复', 'points': [{'title': '报价', 'text': '提供报价。',
                'evidence': [0], 'status': 'direction'}]}}}}, 1)
            w.publish(self.root, ['4'])
        source = w.read(self.root / 'state.json')['sources'][0]
        self.audio = Path(source['audio_path'])
        self.checksum = source['audio_sha256']
        self.contents = self.audio.read_bytes()

    def tearDown(self):
        self.fixture.tearDown()

    def collect(self):
        return a.collect_workspace(self.root, '4', archive_root=self.archive)

    def checkpoint(self):
        # Fail after audio has moved, including a partial temporary document write.
        def failed_copy(source, destination):
            Path(destination).write_bytes(b'partial synthetic document')
            raise OSError('synthetic copy failure')
        with patch.object(a.shutil, 'copy2', side_effect=failed_copy):
            with self.assertRaisesRegex(OSError, 'synthetic copy failure'):
                self.collect()
        self.assertFalse(self.audio.exists())
        self.assertEqual(w.read(self.root / 'state.json')['sources'][0]['audio_path'], str(self.audio))
        index = w.read(a.index_file(self.archive))
        folder = self.archive / index['recordings'][self.checksum]['folder']
        manifest = folder / '.archive.json'
        self.assertEqual((folder / w.read(manifest)['audio_file']).read_bytes(), self.contents)
        self.assertFalse(list(folder.glob('*.tmp')))
        return manifest

    def assert_recovered(self):
        result = self.collect()
        self.assertEqual(Path(result['audio']).read_bytes(), self.contents)
        self.assertEqual(w.file_digest(result['audio']), self.checksum)
        self.assertEqual(w.read(self.root / 'state.json')['sources'][0]['audio_path'], result['audio'])
        self.assertEqual(len(list(self.archive.rglob(self.audio.name))), 1)
        for kind in ('html', 'png', 'md', 'transcript_md'):
            self.assertTrue(Path(result[kind]).is_file())
        return result

    def test_identical_workspace_retry_recovers_after_copy_failure(self):
        self.checkpoint()
        self.assert_recovered()

    def test_legacy_already_moved_checkpoint_recovers_without_new_marker(self):
        manifest = self.checkpoint()
        data = w.read(manifest)
        data.pop('move_source_path')
        w.write(manifest, data)
        self.assert_recovered()

    def test_wrong_index_and_manifest_are_rejected_without_mutation(self):
        manifest = self.checkpoint()
        original_manifest = w.read(manifest)
        original_index = w.read(a.index_file(self.archive))
        state = (self.root / 'state.json').read_bytes()
        changes = [
            {'move_source_path': str(self.audio.with_name('unrelated.m4a'))},
            {'audio_sha256': 'wrong'},
            {'audio_file': '../unrelated.m4a'},
            {'audio_file': str(self.fixture.base / 'unrelated.m4a')},
            {'audio_file': 'absent.m4a'},
        ]
        for changeset in changes:
            with self.subTest(changes=changeset):
                w.write(manifest, {**original_manifest, **changeset})
                before = manifest.read_bytes()
                with self.assertRaises(ValueError):
                    self.collect()
                self.assertEqual(manifest.read_bytes(), before)
                self.assertEqual((self.root / 'state.json').read_bytes(), state)
                self.assertEqual(w.read(a.index_file(self.archive)), original_index)
        w.write(manifest, original_manifest)
        wrong_index = {**original_index, 'recordings': {self.checksum: {'folder': 'missing-folder'}}}
        w.write(a.index_file(self.archive), wrong_index)
        with self.assertRaises(ValueError):
            self.collect()
        self.assertFalse((self.archive / 'missing-folder').exists())
        self.assertEqual((self.root / 'state.json').read_bytes(), state)
        w.write(a.index_file(self.archive), original_index)
        self.assert_recovered()

    def test_recovery_checks_archived_bytes_and_rejects_symlink(self):
        manifest = self.checkpoint()
        archived = manifest.parent / w.read(manifest)['audio_file']
        archived.write_bytes(b'wrong synthetic audio')
        with self.assertRaises(ValueError):
            self.collect()
        self.assertEqual(archived.read_bytes(), b'wrong synthetic audio')
        archived.unlink()
        other = self.fixture.base / 'unrelated.m4a'
        other.write_bytes(self.contents)
        archived.symlink_to(other)
        with self.assertRaises(ValueError):
            self.collect()
        self.assertTrue(archived.is_symlink())
        archived.unlink()
        archived.write_bytes(self.contents)
        self.assert_recovered()

    def test_republish_rename_copy_failure_recovers_original_workspace_path(self):
        first = self.collect()
        old_audio = Path(first['audio'])
        with patch.object(w.minutes_html, 'png_for', side_effect=fake_png):
            revision = w.read(self.root / 'state.json')['revision']
            w.set_analysis(self.root, {'sources': {'4': {'reader': {
                'topic': '重新命名后的合成会议', 'points': [{'title': '报价', 'text': '提供报价。',
                'evidence': [0], 'status': 'direction'}]}}}}, revision)
            w.publish(self.root, ['4'])
        self.audio = old_audio
        manifest = self.checkpoint()
        self.assertNotEqual(manifest.parent, Path(first['folder']))
        self.assertEqual(w.read(manifest)['move_source_path'], str(old_audio))
        self.assertFalse(Path(first['folder']).exists())
        # The original pre-archive path must not be allowed to claim this new move.
        with self.assertRaises(ValueError):
            a.collect(w.read(manifest)['original_audio_path'], root=self.archive,
                      expected_hash=self.checksum)
        result = self.assert_recovered()
        self.assertIn('重新命名后的合成会议', Path(result['html']).read_text())

    def test_missing_source_without_expected_hash_cannot_claim_archive(self):
        self.checkpoint()
        with self.assertRaises(ValueError):
            a.collect(self.audio, root=self.archive)


if __name__ == '__main__':
    unittest.main()
