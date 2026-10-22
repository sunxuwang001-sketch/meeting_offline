"""Offline regression tests for durable Qwen result recovery; no paid calls."""
import argparse
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qwen_job
import transcribe_qwen as api


class RecoveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audio = self.root / 'one.wav'
        with wave.open(str(self.audio), 'wb') as out:
            out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\0\0' * 8000)
        self.args = argparse.Namespace(audio=str(self.audio), duration_seconds=60,
            region='cn', no_diarization=False, keep_dialect=False, filetrans=False,
            hotwords=None, job_dir=str(self.root / 'job'), dry_run=False,
            network_route='direct', poll_seconds=1, timeout_seconds=3,
            confirm_over_2h=False, confirm_upload=True, retry_upload=False, import_result=None)
        self.directory = Path(self.args.job_dir)
        self.task = {'output': {'task_id': 'existing-task', 'task_status': 'SUCCEEDED',
            'results': [{'subtask_status': 'SUCCEEDED', 'transcription_url': 'https://example.invalid/result'}]}}
        self.raw = {'transcripts': [{'text': '成功', 'sentences': [
            {'text': '成功', 'begin_time': 0, 'end_time': 1000, 'speaker_id': 0}]}]}
        self.short = api.normalize_short({'output': {'sentences': [
            {'text': '成功', 'begin_time': 0, 'end_time': 1000, 'speaker_id': 0}]}}, 60)

    def manifest(self, status='completed', **extra):
        _, config, sha, job_id, directory = qwen_job.describe(self.args, api)
        directory.mkdir(exist_ok=True)
        qwen_job.save_json(directory / 'job.json', {
            'device_id': qwen_job.device_context.device_id(), 'status': status,
            'audio_sha256': sha, 'config': config, 'job_id': job_id, **extra})
        return directory

    def offline(self):
        stack = ExitStack()
        for method in ('get_api_key', 'request_json', 'get_upload_policy',
                       'upload_temporary', 'transcribe_short', 'poll_task'):
            stack.enter_context(patch.object(api, method, side_effect=AssertionError('offline: ' + method)))
        return stack

    def long_checkpoint(self):
        self.args.duration_seconds = 400
        self.manifest(task_id='existing-task')
        qwen_job.save_json(self.directory / 'task-response.json', self.task)
        qwen_job.save_json(self.directory / 'transcript-raw.json', self.raw)

    def assert_completed(self):
        self.assertEqual(json.loads((self.directory / 'job.json').read_text())['status'], 'completed')
        self.assertEqual(json.loads((self.directory / 'result.json').read_text())['text'], '成功')

    def test_short_final_write_crash_recovers_without_second_request(self):
        save = qwen_job.save_json
        def fail_final(path, value):
            if Path(path).name == 'result.json':
                raise OSError('injected write failure')
            save(path, value)
        with patch.object(api, 'get_api_key', return_value='fake'), \
             patch.object(api, 'transcribe_short', return_value=self.short) as request, \
             patch.object(qwen_job, 'save_json', side_effect=fail_final):
            with self.assertRaisesRegex(OSError, 'injected'):
                qwen_job.execute(self.args, api)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(json.loads((self.directory / 'job.json').read_text())['status'], 'submitting')
        self.args.confirm_upload = False
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), self.short)
            self.assertEqual(qwen_job.execute(self.args, api), self.short)
        self.assert_completed()

    def test_short_uncertain_submission_with_valid_checkpoint_recovers(self):
        self.manifest(status='uncertain_submission')
        qwen_job.save_json(self.directory / 'response-normalized.json', self.short)
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), self.short)
        self.assert_completed()

    def test_short_invalid_checkpoints_never_read_key_or_resubmit(self):
        wrong_model = {**self.short, 'model': api.LONG_MODEL}
        wrong_duration = {**self.short, 'duration_seconds': 600}
        wrong_route = {**self.short, 'route': 'temporary_storage'}
        bad_segment = {**self.short, 'segments': [None]}
        for value in ('{', 'null', json.dumps(wrong_model), json.dumps(wrong_duration),
                      json.dumps(wrong_route), json.dumps(bad_segment),
                      json.dumps({**self.short, 'segments': []})):
            with self.subTest(value=value):
                self.manifest(status='submitting')
                (self.directory / 'response-normalized.json').write_text(value)
                with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '检查点无效.*不会重新提交'):
                    qwen_job.execute(self.args, api)
                self.assertFalse((self.directory / 'result.json').exists())

    def test_uncertain_without_checkpoint_still_refuses_duplicate(self):
        self.manifest(status='submitting')
        with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '结果不确定'):
            qwen_job.execute(self.args, api)

    def test_corrupt_long_final_recovers_and_preserves_damaged_bytes(self):
        self.long_checkpoint()
        (self.directory / 'result.json').write_text('{')
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '成功')
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '成功')
        quarantines = list(self.directory.glob('result.corrupt-*.json'))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0].read_text(), '{')
        self.assert_completed()

    def test_semantically_invalid_short_final_recovers(self):
        self.manifest()
        qwen_job.save_json(self.directory / 'result.json', {**self.short, 'segments': [None]})
        qwen_job.save_json(self.directory / 'response-normalized.json', self.short)
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), self.short)
        self.assertEqual(len(list(self.directory.glob('result.corrupt-*.json'))), 1)

    def test_corrupt_final_explicit_import_beats_stale_cloud_checkpoint(self):
        self.long_checkpoint()
        (self.directory / 'result.json').write_text('{')
        (self.directory / 'transcript-raw.json').write_text('null')
        imported = api.normalize_long(self.raw, self.task, 400)
        self.args.import_result = str(self.root / 'import.json')
        qwen_job.save_json(Path(self.args.import_result), imported)
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), imported)
        self.assertEqual(json.loads((self.directory / 'job.json').read_text())['origin'], 'imported_existing_transcript')

    def test_imported_origin_never_silently_switches_to_provider_checkpoint(self):
        self.manifest(origin='imported_existing_transcript')
        (self.directory / 'result.json').write_text('{')
        qwen_job.save_json(self.directory / 'response-normalized.json', self.short)
        with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '显式重新导入'):
            qwen_job.execute(self.args, api)
        self.assertFalse((self.directory / 'result.json').exists())

    def test_corrupt_final_without_checkpoint_fails_on_every_retry(self):
        for duration in (60, 400):
            with self.subTest(duration=duration):
                self.args.duration_seconds = duration
                self.manifest()
                (self.directory / 'result.json').write_text('{')
                for _ in range(2):
                    with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '没有可验证.*不会重新提交'):
                        qwen_job.execute(self.args, api)

    def test_invalid_long_checkpoints_fail_offline_not_wrong_source(self):
        variants = [('task-response.json', '{'), ('transcript-raw.json', '[]'),
                    ('transcript-raw.json', '{"transcripts":[]}')]
        for updates in ({'task_status': 'FAILED'}, {'task_id': 'another-task'},
                        {'results': []}, {'results': [{'subtask_status': 'FAILED', 'transcription_url': 'https://example.invalid'}]}):
            task = copy.deepcopy(self.task)
            task['output'].update(updates)
            variants.append(('task-response.json', json.dumps(task)))
        for filename, value in variants:
            with self.subTest(filename=filename, value=value):
                self.long_checkpoint()
                (self.directory / 'result.json').write_text('{')
                (self.directory / filename).write_text(value)
                with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '检查点无效.*不会重新提交'):
                    qwen_job.execute(self.args, api)
                self.assertFalse((self.directory / 'result.json').exists())

    def test_valid_final_survives_manifest_completion_write_failure(self):
        self.manifest(status='submitting')
        qwen_job.save_json(self.directory / 'result.json', self.short)
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), self.short)
        self.assert_completed()

    def test_corrupt_uncompleted_final_recovers_from_local_checkpoint(self):
        self.manifest(status='submitting')
        (self.directory / 'result.json').write_text('{')
        qwen_job.save_json(self.directory / 'response-normalized.json', self.short)
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), self.short)
        self.assert_completed()
        self.assertNotIn('result_cache_invalid', json.loads((self.directory / 'job.json').read_text()))

    def test_corrupt_uncompleted_final_without_checkpoint_blocks_all_retries(self):
        for status in ('planned', 'submitting', 'failed'):
            with self.subTest(status=status):
                self.manifest(status=status)
                (self.directory / 'result.json').write_text('{')
                for _ in range(2):
                    with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '没有可验证.*不会重新提交'):
                        qwen_job.execute(self.args, api)

    def test_invalid_explicit_import_does_not_fall_back_to_cloud_checkpoint(self):
        self.manifest(origin='imported_existing_transcript')
        (self.directory / 'result.json').write_text('{')
        qwen_job.save_json(self.directory / 'response-normalized.json', self.short)
        self.args.import_result = str(self.root / 'import.json')
        for value in ('{', json.dumps({**self.short, 'route': 'temporary_storage'})):
            Path(self.args.import_result).write_text(value)
            with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '导入结果无效'):
                qwen_job.execute(self.args, api)

    def test_wrong_recording_manifest_is_not_recovered(self):
        self.manifest(status='submitting')
        qwen_job.save_json(self.directory / 'response-normalized.json', self.short)
        self.audio.write_bytes(b'changed recording')
        with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '另一段录音'):
            qwen_job.execute(self.args, api)


if __name__ == '__main__':
    unittest.main()
