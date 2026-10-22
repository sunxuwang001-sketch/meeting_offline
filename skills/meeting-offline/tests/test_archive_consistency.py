"""Synthetic publication preservation and crash-safe archive directory relocation."""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_meeting as a
import audit
import workspace as w
import test_workspace
from test_obsidian import fake_png


class ArchiveConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_workspace.WorkspaceTests()
        self.fixture.setUp()
        self.root, self.base = self.fixture.root, self.fixture.base
        self.archive = self.base / 'archive'
        self.png = patch.object(w.minutes_html, 'png_for', side_effect=fake_png)
        self.png.start()
        state = w.read(self.root / 'state.json')
        for source in state['sources']:
            Path(source['audio_path']).write_bytes(('synthetic-' + source['source_id']).encode())
            source['audio_sha256'] = w.file_digest(source['audio_path'])
        w.write(self.root / 'state.json', state)
        self.analysis = {'sources': {sid: {'reader': {'topic': '会议' + sid, 'points': [
            {'title': '报价', 'text': '提供报价。', 'evidence': [0], 'status': 'direction'}]}}
            for sid in ('4', '5')}}
        w.set_analysis(self.root, self.analysis, 1)
        w.publish(self.root)

    def tearDown(self):
        self.png.stop()
        self.fixture.tearDown()

    def collect(self, sid='4'):
        return a.collect_workspace(self.root, sid, archive_root=self.archive)

    def test_one_publish_sequential_and_repeated_archives_keep_reviewed_snapshot(self):
        latest = (self.root / 'exports/latest.json').read_bytes()
        publication = w.read(self.root / 'publication.json')
        first = self.collect()
        self.assertEqual(first, self.collect())
        second = self.collect('5')
        self.assertEqual(second, self.collect('5'))
        self.assertEqual((self.root / 'exports/latest.json').read_bytes(), latest)
        current = w.read(self.root / 'publication.json')
        for key in ('export_version', 'revision', 'files'):
            self.assertEqual(current[key], publication[key])
        self.assertEqual(len(list(self.archive.rglob('*.m4a'))), 2)
        self.assertTrue(audit.is_checked(self.root))

    def test_archived_html_explicit_speaker_edit_can_be_imported(self):
        first = self.collect()
        path = Path(first['html'])
        text = path.read_text()
        pattern = r'(id="meeting-speaker-edits">)(.*?)(</script>)'
        payload = json.loads(re.search(pattern, text, re.S)[2])
        payload['mappings'] = [{'key': '4-A', 'name': '林悦明'}]
        path.write_text(re.sub(pattern, lambda m: m[1] + json.dumps(payload, ensure_ascii=False) + m[3], text, flags=re.S))
        w.import_html(self.root, path, w.read(self.root / 'state.json')['revision'])
        self.assertEqual(w.read(self.root / 'state.json')['speakers'][0]['role_name'], '林悦明')

    def test_manually_resolved_audit_stays_bound_to_exact_reviewed_export(self):
        self.analysis['sources']['4']['reader']['points'][0]['text'] = '报价为 229 元。'
        w.set_analysis(self.root, self.analysis, 2)
        problems = audit.audit(self.root)
        self.assertTrue(problems)
        audit.resolve(self.root, {p['id']: {'resolution': 'user_confirmed', 'note': '合成用户确认'} for p in problems}, 3)
        w.publish(self.root)
        reviewed = (self.root / 'semantic_review.json').read_bytes()
        self.collect(); self.collect('5'); self.collect()
        self.assertEqual((self.root / 'semantic_review.json').read_bytes(), reviewed)
        self.assertTrue(audit.is_checked(self.root))

    def test_real_content_change_partial_publish_and_tampered_file_still_rejected(self):
        self.collect()
        self.analysis['sources']['4']['reader']['topic'] = '真实内容修改'
        w.set_analysis(self.root, self.analysis, 2)
        with self.assertRaisesRegex(ValueError, '过期'):
            self.collect('5')
        w.publish(self.root, ['4'])
        with self.assertRaisesRegex(ValueError, '尚未发布'):
            self.collect('5')
        pub = w.read(self.root / 'publication.json')
        Path(pub['files']['4']['html']['path']).write_text('tampered')
        with self.assertRaisesRegex(ValueError, '发布文件已改变'):
            self.collect()

    def renamed_checkpoint(self, failure_at):
        first = self.collect()
        old = Path(first['folder'])
        self.analysis['sources']['4']['reader']['topic'] = '重命名后的会议'
        w.set_analysis(self.root, self.analysis, 2)
        w.publish(self.root)
        real_write = w.write
        def fail(path, data):
            if not old.exists() and Path(path).name == failure_at:
                raise OSError('synthetic metadata failure after rename')
            return real_write(path, data)
        with patch.object(w, 'write', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'after rename'):
                self.collect()
        self.assertFalse(old.exists())
        source = w.read(self.root / 'state.json')['sources'][0]
        checkpoint = a.rename_intent_file(self.archive, source['audio_sha256'])
        self.assertTrue(checkpoint.is_file())
        self.assertEqual(len(list(self.archive.rglob('4.m4a'))), 1)
        return first, source, checkpoint

    def assert_retry(self, checkpoint):
        result = self.collect()
        self.assertEqual(Path(result['audio']).read_bytes(), b'synthetic-4')
        self.assertEqual(len(list(self.archive.rglob('4.m4a'))), 1)
        self.assertFalse(checkpoint.exists())
        self.assertEqual(self.collect(), result)
        self.assertIn('重命名后的会议', Path(result['html']).read_text())

    def test_rename_then_manifest_write_failure_retry(self):
        _, _, checkpoint = self.renamed_checkpoint('.archive.json')
        self.assert_retry(checkpoint)

    def test_rename_then_index_write_failure_retry(self):
        _, _, checkpoint = self.renamed_checkpoint('.archive-index.json')
        self.assert_retry(checkpoint)

    def test_workspace_audio_basename_change_is_rejected_before_move(self):
        source = w.read(self.root / 'state.json')['sources'][0]
        path = Path(source['audio_path'])
        with patch.object(a, 'unique_path', side_effect=lambda p, identity: p.with_name('changed-' + p.name)):
            with self.assertRaisesRegex(ValueError, '文件名'):
                self.collect()
        self.assertTrue(path.is_file())
        self.assertFalse(list(self.archive.rglob('*.m4a')))

    def test_intent_write_failure_leaves_old_folder_and_retry_works(self):
        first = self.collect()
        self.analysis['sources']['4']['reader']['topic'] = '重命名后的会议'
        w.set_analysis(self.root, self.analysis, 2)
        w.publish(self.root)
        real_write = w.write
        def fail(path, data):
            if Path(path).name.startswith('.archive-rename-'):
                raise OSError('synthetic intent write failure')
            return real_write(path, data)
        with patch.object(w, 'write', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'intent write'):
                self.collect()
        self.assertTrue(Path(first['audio']).is_file())
        self.assertEqual(len(list(self.archive.rglob('4.m4a'))), 1)
        result = self.collect()
        self.assertEqual(Path(result['audio']).read_bytes(), b'synthetic-4')
        self.assertEqual(self.collect(), result)

    def test_rename_intent_rejects_wrong_hash_source_manifest_and_links(self):
        _, source, checkpoint = self.renamed_checkpoint('.archive.json')
        original = w.read(checkpoint)
        for key, value in [('audio_sha256', 'wrong'), ('source_path', '/unrelated.m4a'), ('new_folder', '../escape')]:
            with self.subTest(key=key):
                w.write(checkpoint, {**original, key: value})
                with self.assertRaises(ValueError): self.collect()
        w.write(checkpoint, original)
        folder = self.archive / original['new_folder']
        manifest = folder / '.archive.json'
        saved = w.read(manifest)
        w.write(manifest, {**saved, 'audio_sha256': 'wrong'})
        with self.assertRaises(ValueError): self.collect()
        w.write(manifest, saved)
        audio = folder / original['audio_file']
        content = audio.read_bytes()
        audio.write_bytes(b'wrong')
        with self.assertRaises(ValueError): self.collect()
        audio.unlink()
        other = self.base / 'other.m4a'; other.write_bytes(content)
        audio.symlink_to(other)
        with self.assertRaises(ValueError): self.collect()
        audio.unlink(); audio.write_bytes(content)
        moved = self.archive / 'temporary-real-directory'
        folder.rename(moved); folder.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError): self.collect()
        folder.unlink(); moved.rename(folder)
        self.assert_retry(checkpoint)


if __name__ == '__main__': unittest.main()
