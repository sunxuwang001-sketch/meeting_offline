"""Review follow-up: time semantics and read-only recovery, using synthetic data only."""
import copy
import json
from pathlib import Path
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit
import qwen_job
import transcribe_qwen as api
import test_qwen_recovery_regressions as fixtures


class TimeReviewTests(unittest.TestCase):
    def test_ordinals_are_not_times_even_with_time_like_suffix(self):
        for text in ('第三点完成', '第3点开始', '第十三点结束', '第 三点前', '第3点半'):
            with self.subTest(text=text):
                self.assertFalse(any(k.startswith('time') for k, _ in audit.facts(text)))

    def test_long_words_are_not_single_character_time_context(self):
        for tail in ('前提', '前景', '前期', '整改', '整理', '整体', '整合', '整顿', '后续', '后果'):
            with self.subTest(tail=tail):
                self.assertEqual(audit.facts('三点' + tail), {('number', '3')})
        for tail in ('前交付', '后提交', '整', '准时开始', '左右完成'):
            with self.subTest(tail=tail):
                self.assertEqual(audit.facts('三点' + tail), {('time_ambiguous', '3:00')})

    def test_range_inherits_period_but_not_across_unrelated_text(self):
        for join in ('到', '至', '–', '—', '-', '～'):
            with self.subTest(join=join):
                self.assertEqual(audit.facts('下午三点' + join + '五点'),
                                 {('time', '15:00'), ('time', '17:00')})
        self.assertEqual(audit.facts('下午三点半到五点半'), {('time', '15:30'), ('time', '17:30')})
        self.assertEqual(audit.facts('下午三点到上午五点'), {('time', '15:00'), ('time', '5:00')})
        self.assertEqual(audit.facts('下午三点讨论五点建议'), {('time', '15:00'), ('number', '5')})
        self.assertEqual(audit.facts('下午三点，五点交付'), {('time', '15:00'), ('time_ambiguous', '5:00')})
        self.assertEqual(audit.check_claim('m', 'x', '15:00到17:00', [0], ['下午三点到五点']), [])
        self.assertTrue(audit.check_claim('m', 'x', '15:00到05:00', [0], ['下午三点到五点']))

    def test_night_early_hours_do_not_become_afternoon(self):
        for hour, chinese in enumerate('一二三四', 1):
            for source in ('晚上' + chinese + '点', '晚上' + str(hour) + ':00'):
                with self.subTest(source=source):
                    self.assertEqual(audit.facts(source), {('time', f'{hour}:00')})
                    self.assertTrue(audit.check_claim('m', 'x', f'{hour + 12}:00', [0], [source]))
        self.assertEqual(audit.facts('晚上九点'), {('time', '21:00')})
        self.assertEqual(audit.facts('晚上十二点'), {('time', '0:00')})


class JobReviewTests(unittest.TestCase):
    setUp = fixtures.RecoveryRegressionTests.setUp
    manifest = fixtures.RecoveryRegressionTests.manifest
    offline = fixtures.RecoveryRegressionTests.offline

    def damaged_ready(self, task=True):
        self.args.duration_seconds = 400
        self.args.confirm_upload = False
        self.manifest(status='result_ready', task_id='existing-task', audio_channels=[1])
        (self.directory / 'result.json').write_text('{')
        (self.directory / 'transcript-raw.json').write_text('{')
        if task:
            qwen_job.save_json(self.directory / 'task-response.json', self.task)

    def read_only(self, response):
        stack = ExitStack()
        stack.enter_context(patch.object(api, 'get_api_key', return_value='synthetic'))
        for name in ('get_upload_policy', 'upload_temporary', 'transcribe_short'):
            stack.enter_context(patch.object(api, name, side_effect=AssertionError('no paid submission: ' + name)))
        calls = []
        def request(url, **kwargs):
            self.assertEqual(kwargs.get('method', 'GET'), 'GET')
            calls.append(url)
            return response(url)
        stack.enter_context(patch.object(api, 'request_json', side_effect=request))
        return stack, calls

    def test_damaged_final_and_raw_redownload_validated_existing_result(self):
        self.damaged_ready()
        stack, calls = self.read_only(lambda url: self.raw)
        with stack:
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '成功')
        self.assertEqual(calls, ['https://example.invalid/result'])
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '成功')
        self.assertEqual(len(list(self.directory.glob('result.corrupt-*.json'))), 1)

    def test_missing_task_checkpoint_gets_only_same_task_then_result(self):
        self.damaged_ready(task=False)
        stack, calls = self.read_only(lambda url: self.task if '/tasks/' in url else self.raw)
        with stack:
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '成功')
        self.assertEqual(calls, [api.region_base('cn') + '/tasks/existing-task', 'https://example.invalid/result'])

    def test_failed_download_retry_never_posts(self):
        self.damaged_ready()
        def fail(url):
            raise api.TranscriptionError('synthetic expired result')
        stack, calls = self.read_only(fail)
        with stack:
            for _ in range(2):
                with self.assertRaisesRegex(api.TranscriptionError, 'synthetic expired'):
                    qwen_job.execute(self.args, api)
        self.assertEqual(calls, ['https://example.invalid/result'] * 2)

    def test_invalid_task_source_never_downloads(self):
        for checkpoint in (True, False):
            with self.subTest(checkpoint=checkpoint):
                self.damaged_ready(task=checkpoint)
                bad = copy.deepcopy(self.task)
                bad['output']['task_id'] = 'other-task'
                path = self.directory / 'task-response.json'
                if checkpoint:
                    qwen_job.save_json(path, bad)
                elif path.exists():
                    path.unlink()
                stack, calls = self.read_only(lambda url: bad)
                with stack, self.assertRaises(api.TranscriptionError):
                    qwen_job.execute(self.args, api)
                self.assertEqual(calls, [] if checkpoint else [api.region_base('cn') + '/tasks/existing-task'])
                self.assertFalse((self.directory / 'result.json').exists())

    def test_poll_timeout_retry_uses_existing_task_without_resubmission(self):
        self.damaged_ready(task=False)
        def fail(url):
            raise api.TranscriptionError('synthetic timeout')
        stack, calls = self.read_only(fail)
        with stack:
            for _ in range(2):
                with self.assertRaisesRegex(api.TranscriptionError, 'synthetic timeout'):
                    qwen_job.execute(self.args, api)
        self.assertEqual(calls, [api.region_base('cn') + '/tasks/existing-task'] * 2)
        stack, calls = self.read_only(lambda url: self.task if '/tasks/' in url else self.raw)
        with stack:
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '成功')

    def test_fetched_task_must_have_one_successful_source(self):
        for update in ({'task_status': 'FAILED'}, {'results': []},
                       {'results': self.task['output']['results'] * 2}):
            with self.subTest(update=update):
                self.damaged_ready(task=False)
                bad = copy.deepcopy(self.task)
                bad['output'].update(update)
                stack, calls = self.read_only(lambda url: bad)
                with stack, self.assertRaises(api.TranscriptionError):
                    qwen_job.execute(self.args, api)
                self.assertEqual(calls, [api.region_base('cn') + '/tasks/existing-task'])
                self.assertFalse((self.directory / 'result.json').exists())

    def test_ready_allows_explicit_valid_import_without_network(self):
        self.damaged_ready()
        imported = api.normalize_long(self.raw, self.task, 400)
        self.args.import_result = str(self.root / 'import.json')
        qwen_job.save_json(Path(self.args.import_result), imported)
        with self.offline():
            self.assertEqual(qwen_job.execute(self.args, api), imported)
        self.assertEqual(json.loads((self.directory / 'job.json').read_text())['origin'], 'imported_existing_transcript')

    def test_ready_rejects_invalid_import_without_fallback(self):
        self.damaged_ready()
        self.args.import_result = str(self.root / 'import.json')
        imported = api.normalize_long(self.raw, self.task, 400)
        for update in ({'duration_seconds': 40}, {'model': api.SHORT_MODEL}, {'segments': []}):
            with self.subTest(update=update):
                qwen_job.save_json(Path(self.args.import_result), {**imported, **update})
                with self.offline(), self.assertRaisesRegex(api.TranscriptionError, '导入结果无效'):
                    qwen_job.execute(self.args, api)


if __name__ == '__main__':
    unittest.main()
