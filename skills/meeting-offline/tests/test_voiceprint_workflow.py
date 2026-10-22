"""Synthetic end-to-end gates; no real audio, model, or browser required."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit
import archive_meeting
import device_context
import minutes_html
import workspace as w
import voiceprint
import test_audit as fixtures
from test_minutes_html import SeedParser


class VoiceprintWorkflowTests(unittest.TestCase):
    def setUp(self):
        fixtures.AuditFlowTests.setUp(self)
        w.set_analysis(self.root, self.analysis(), self.revision())

    tearDown = fixtures.AuditFlowTests.tearDown
    analysis = fixtures.AuditFlowTests.analysis
    revision = fixtures.AuditFlowTests.revision

    def state(self):
        return w.read(self.root / 'state.json')

    def pending(self, matched=True):
        state = self.state()
        for s in state['speakers']:
            s['role_confirmed'] = False
            s['role_source'] = 'voice_match' if matched else 'alias'
            if matched:
                s['voice_similarity'] = .91
        w.write(self.root / 'state.json', state)
        return state

    def payload(self, mappings=None):
        state = self.state()
        return {'schema_version': 1, 'workspace_id': w.workspace_identity(self.root, state),
                'revision': state['revision'], 'source_id': 'm', 'mappings': mappings or []}

    def html(self, payload, extra=''):
        path = self.base / 'edited.html'
        safe = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e')
        path.write_text('<script type="application/json" id="meeting-speaker-edits">' + safe + '</script>' + extra,
                        encoding='utf-8')
        return path

    def test_untouched_auto_names_do_not_confirm_or_write_libraries(self):
        self.pending()
        before = (self.root / 'state.json').read_bytes()
        memory_path = Path(w.memory_path(self.state()))
        memory_before = memory_path.read_bytes() if memory_path.exists() else None
        with mock.patch.object(voiceprint, 'enroll_confirmed') as enroll, mock.patch.object(w, 'confirmed_memory_upsert') as upsert:
            result = w.import_html(self.root, self.html(self.payload()), self.revision())
        self.assertEqual(result, {'revision': self.revision(), 'imported': 0})
        enroll.assert_not_called(); upsert.assert_not_called()
        self.assertEqual((self.root / 'state.json').read_bytes(), before)
        self.assertEqual(memory_path.read_bytes() if memory_path.exists() else None, memory_before)

    def test_only_explicit_mapping_is_applied_and_other_html_state_is_ignored(self):
        self.pending()
        before = self.state()
        analysis_before = (self.root / 'analysis.json').read_bytes()
        extra = '<script id="minutes-data" type="application/json">' + json.dumps({
            'revision': 999, 'analysis': {'forged': True}, 'speakers': [{'key': 'm-B', 'role_confirmed': True}],
            'audit_issues': [], 'memory_store': '/tmp/not-authorized'}) + '</script>'
        with mock.patch.object(voiceprint, 'enroll_confirmed') as enroll:
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '确认姓名'}]), extra), self.revision())
        state = self.state()
        a, b = state['speakers']
        self.assertEqual(a['display_name'], '确认姓名'); self.assertTrue(a['role_confirmed'])
        self.assertEqual(b, before['speakers'][1])
        self.assertEqual(state['revision'], before['revision'] + 1)
        self.assertEqual((self.root / 'analysis.json').read_bytes(), analysis_before)
        self.assertEqual(state['sources'], before['sources'])
        enroll.assert_not_called()  # Voice is disabled: explicit identity still saves.
        self.assertTrue(any(p['name'] == '确认姓名' for p in w.read(Path(w.memory_path(state)))['roles']))

    def test_import_rejects_foreign_stale_unknown_and_malicious_payloads_atomically(self):
        self.pending()
        valid = self.payload([{'key': 'm-A', 'name': '确认姓名'}])
        cases = []
        for key, value in [('workspace_id', 'foreign'), ('revision', 0), ('source_id', 'unknown'),
                           ('analysis', {}), ('role_confirmed', True), ('mappings', [{'key': 'm-A', 'name': '姓名', 'role_confirmed': True}]),
                           ('mappings', [{'key': 'unknown-A', 'name': '姓名'}]), ('mappings', [{'key': 'm-A', 'name': ''}])]:
            item = copy.deepcopy(valid); item[key] = value; cases.append(item)
        before = (self.root / 'state.json').read_bytes()
        with mock.patch.object(voiceprint, 'enroll_confirmed') as enroll, mock.patch.object(w, 'confirmed_memory_upsert') as upsert:
            for payload in cases:
                with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                    w.import_html(self.root, self.html(payload), self.revision())
                self.assertEqual((self.root / 'state.json').read_bytes(), before)
        enroll.assert_not_called(); upsert.assert_not_called()

    def test_unassigned_cannot_be_imported_even_with_legacy_identity(self):
        state = self.pending()
        state['speakers'][0].update(key='m-UNASSIGNED', diarization_unassigned=True, identity_group='old-person')
        w.write(self.root / 'state.json', state)
        with self.assertRaisesRegex(ValueError, '未分离'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-UNASSIGNED', 'name': '姓名'}])), self.revision())
        self.assertEqual(minutes_html.shown_name(state['speakers'][0]), '林悦')

    def test_default_off_alias_full_html_and_confirmation_fallbacks(self):
        self.pending(matched=False)
        with mock.patch.object(device_context, 'sandbox', return_value=False), mock.patch.object(device_context, 'minutes_preferences', return_value={}):
            w.set_analysis(self.root, self.analysis(), self.revision())
            files = w.read(self.root / 'exports/latest.json')['files']['m']
            self.assertIn('html', files); self.assertIn('png', files)
            parser = SeedParser(); parser.feed(Path(files['html']).read_text())
            self.assertEqual(json.loads(parser.seed)['todos'][0]['owner'], '说话人 A')
        for sandbox, prefs in [(True, {}), (False, {'voiceprint': {'enabled': True}})]:
            with self.subTest(sandbox=sandbox), mock.patch.object(device_context, 'sandbox', return_value=sandbox), mock.patch.object(device_context, 'minutes_preferences', return_value=prefs):
                files = w.export(self.root)['files']['m']
                self.assertNotIn('html', files)
        self.pending(matched=True)
        with mock.patch.object(device_context, 'sandbox', return_value=False), mock.patch.object(device_context, 'minutes_preferences', return_value={'voiceprint': {'enabled': True}}):
            self.assertIn('html', w.export(self.root)['files']['m'])

    def test_unchanged_display_alias_reuses_profile_and_replay_is_stale(self):
        for field in ('identity_group', 'voice_profile_id'):
            with self.subTest(field=field):
                state = self.pending()
                speaker = state['speakers'][0]
                speaker.pop('identity_group', None)
                speaker.pop('voice_profile_id', None)
                speaker[field] = 'person-lin'
                w.write(self.root / 'state.json', state)
                memory_path = Path(w.memory_path(state))
                w.write(memory_path, {'schema_version': 1, 'device_id': device_context.device_id(),
                    'roles': [{'id': 'person-lin', 'name': '林悦', 'entity_type': 'person', 'scope': '测试'}],
                    'terms': [], 'history': []})
                with mock.patch.object(w, 'display_names', return_value={'林悦': '我'}), mock.patch.object(voiceprint, 'enroll_confirmed'):
                    w.export(self.root)
                    page = self.html(self.payload([{'key': 'm-A', 'name': '我'}]))
                    w.import_html(self.root, page, self.revision())
                    self.assertEqual(self.state()['speakers'][0]['identity_group'], 'person-lin')
                    self.assertEqual(len(w.read(memory_path)['roles']), 1)
                    before = memory_path.read_bytes()
                    with self.assertRaises(ValueError):
                        w.import_html(self.root, page, self.revision())
                    self.assertEqual(memory_path.read_bytes(), before)

    def test_unchanged_unknown_labels_cannot_create_person_but_rename_can(self):
        for accepted in (False, True):
            with self.subTest(accepted_unknown=accepted):
                state = self.state()
                speaker = state['speakers'][0]
                speaker.update(role_confirmed=accepted, accepted_unknown=accepted, role_source='user' if accepted else 'alias',
                               role_name=minutes_html.UNKNOWN_ROLE if accepted else '', display_name=None)
                speaker.pop('identity_group', None)
                speaker.pop('voice_profile_id', None)
                w.write(self.root / 'state.json', state)
                w.export(self.root)
                label = minutes_html.shown_name(speaker)
                before = (self.root / 'state.json').read_bytes()
                with mock.patch.object(w, 'confirmed_memory_upsert') as upsert, mock.patch.object(voiceprint, 'enroll_confirmed') as enroll:
                    result = w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': label}])), self.revision())
                    self.assertEqual(result['imported'], 0)
                    upsert.assert_not_called(); enroll.assert_not_called()
                self.assertEqual((self.root / 'state.json').read_bytes(), before)
                with mock.patch.object(voiceprint, 'enroll_confirmed'):
                    w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '用户填写的姓名'}])), self.revision())
                self.assertEqual(self.state()['speakers'][0]['display_name'], '用户填写的姓名')
                self.assertTrue(self.state()['speakers'][0]['role_confirmed'])

    def test_ambiguous_old_names_preserve_prose_but_unique_key_can_sync(self):
        state = self.state()
        for speaker in state['speakers']:
            speaker.update(role_name='林悦', display_name='林悦')
        w.write(self.root / 'state.json', state)
        analysis = self.analysis()
        analysis['sources']['m']['reader']['summary'] = '林悦：提供报价。m-A：准备材料。'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        self.assertEqual(w.read(self.root / 'analysis.json')['sources']['m']['reader']['summary'], '林悦：提供报价。张三：准备材料。')
        files = w.read(self.root / 'exports/latest.json')['files']['m']
        self.assertNotIn('html', files); self.assertIn('reader_error', files)

    def test_fallback_core_points_and_source_title_sync_without_old_name_leak(self):
        state = self.state()
        state['speakers'][0]['core_points'] = [{'text': '林悦：报价。'}, '林悦：材料。']
        w.write(self.root / 'state.json', state)
        analysis = self.analysis()
        analysis['sources']['m']['title'] = '林悦：例会'
        analysis['sources']['m']['reader'] = {'points': []}
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        self.assertEqual(self.state()['speakers'][0]['core_points'], [{'text': '张三：报价。'}, '张三：材料。'])
        self.assertEqual(w.read(self.root / 'analysis.json')['sources']['m']['title'], '张三：例会')
        files = w.read(self.root / 'exports/latest.json')['files']['m']
        parser = SeedParser(); parser.feed(Path(files['html']).read_text())
        document = json.loads(parser.seed)
        self.assertNotIn('林悦', json.dumps(document, ensure_ascii=False))
        self.assertIn('张三：报价。', document['points'][0]['text'])

    def test_voice_match_in_one_recording_does_not_unlock_another(self):
        state = self.pending(matched=False)
        source = copy.deepcopy(state['sources'][0]); source['source_id'] = 'n'
        state['sources'].append(source)
        other = copy.deepcopy(state['speakers'])
        for speaker in other:
            speaker['source_id'] = 'n'; speaker['key'] = speaker['key'].replace('m-', 'n-')
        state['speakers'].extend(other)
        state['speakers'][0]['role_source'] = 'voice_match'
        w.write(self.root / 'state.json', state)
        analysis = self.analysis()
        analysis['sources']['n'] = copy.deepcopy(analysis['sources']['m'])
        analysis['sources']['n']['actions'][0]['owner'] = 'n-A'
        with mock.patch.object(device_context, 'sandbox', return_value=False), mock.patch.object(device_context, 'minutes_preferences', return_value={'voiceprint': {'enabled': True}}):
            self.assertFalse(w.requires_role_review(state, 'm'))
            self.assertTrue(w.requires_role_review(state, 'n'))
            w.set_analysis(self.root, analysis, self.revision())
        files = w.read(self.root / 'exports/latest.json')['files']
        self.assertIn('html', files['m']); self.assertNotIn('html', files['n'])

    def test_malformed_later_mapping_and_skipped_name_collision_do_not_write(self):
        state = self.pending()
        state['speakers'][1].pop('identity_group', None)
        state['speakers'][1].pop('voice_profile_id', None)
        w.write(self.root / 'state.json', state)
        before = (self.root / 'state.json').read_bytes()
        cases = [[{'key': 'm-A', 'name': '新名字'}, None],
                 [{'key': 'm-A', 'name': '陈思远'}, {'key': 'm-B', 'name': '陈思远'}]]
        with mock.patch.object(w, 'confirmed_memory_upsert') as upsert, mock.patch.object(voiceprint, 'enroll_confirmed') as enroll:
            for mappings in cases:
                with self.subTest(mappings=mappings), self.assertRaises(ValueError):
                    w.import_html(self.root, self.html(self.payload(mappings)), self.revision())
            upsert.assert_not_called(); enroll.assert_not_called()
        self.assertEqual((self.root / 'state.json').read_bytes(), before)

    def test_explicit_single_speaker_confirmation_restores_owner_but_never_voiceprint(self):
        w.set_analysis(self.root, {'sources': {}}, self.revision())
        state = self.state()
        speaker = state['speakers'][0]
        speaker.update(key='m-UNASSIGNED', diarization_unassigned=True, role_confirmed=False,
                       identity_group=None, role_name='', display_name=None)
        w.write(self.root / 'state.json', state)
        with mock.patch.object(voiceprint, 'status', return_value={'enabled': True}), mock.patch.object(voiceprint, 'speaker_vectors', return_value=({}, {})) as vectors:
            w.save_roles(self.root, [{'key': 'm-UNASSIGNED', 'name': '林悦', 'entity_type': 'person',
                                     'confirm_single_speaker': True}], self.revision())
        vectors.assert_not_called()
        analysis = self.analysis()
        analysis['sources']['m']['actions'][0]['owner'] = 'm-UNASSIGNED'
        w.set_analysis(self.root, analysis, self.revision())
        files = w.read(self.root / 'exports/latest.json')['files']['m']
        parser = SeedParser(); parser.feed(Path(files['html']).read_text())
        document = json.loads(parser.seed)
        self.assertIn('林悦', document['participants'])
        self.assertEqual(document['todos'][0]['owner'], '林悦')
        self.assertFalse(any(row['key'] == 'm-UNASSIGNED' for row in document['speakers']))

    def test_html_reserved_labels_and_same_batch_collision_are_rejected(self):
        for label in ('身份未确认', '说话人未分离', '说话人 X', '说话人ABC', 'm-B'):
            with self.subTest(label=label), self.assertRaises(ValueError):
                w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': label}])), self.revision())
        with self.assertRaisesRegex(ValueError, '同一个名字'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '同名'}, {'key': 'm-B', 'name': '同名'}])), self.revision())

    def test_html_rename_syncs_bounded_prose_and_records_html_confirmation(self):
        analysis = self.analysis()
        reader = analysis['sources']['m']['reader']
        reader['summary'] = '林悦：提供报价。'
        reader['points'][0]['title'] = '林悦'
        reader['points'][0]['text'] = '林悦：提供报价。'
        reader['todos'][0]['task'] = '林悦：提供报价。'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        reader = w.read(self.root / 'analysis.json')['sources']['m']['reader']
        self.assertEqual(reader['summary'], '张三：提供报价。')
        self.assertEqual(reader['points'][0]['title'], '张三')
        self.assertEqual(reader['points'][0]['text'], '张三：提供报价。')
        self.assertEqual(reader['todos'][0]['task'], '张三：提供报价。')
        profile = next(p for p in w.read(Path(w.memory_path(self.state())))['roles'] if p['name'] == '张三')
        self.assertIn('纪要网页', profile['confirmation'])
        self.assertIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])

    def test_ambiguous_prose_is_preserved_and_blocks_full_html_until_reauthored(self):
        analysis = self.analysis()
        analysis['sources']['m']['reader']['summary'] = '林悦认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        self.assertEqual(w.read(self.root / 'analysis.json')['sources']['m']['reader']['summary'], '林悦认为报价可行。')
        files = w.read(self.root / 'exports/latest.json')['files']['m']
        self.assertNotIn('html', files); self.assertIn('reader_error', files)
        self.assertIn('林悦', self.state()['reader_stale_sources']['m']['old_names'])
        w.set_analysis(self.root, analysis, self.revision())
        still_blocked = w.read(self.root / 'exports/latest.json')['files']['m']
        self.assertNotIn('html', still_blocked)
        self.assertIn('林悦', still_blocked['reader_error'])
        analysis['sources']['m']['reader']['summary'] = '张三认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        self.assertIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])

    def test_rename_analysis_and_state_roll_back_on_commit_write_failure(self):
        analysis = self.analysis()
        analysis['sources']['m']['reader']['summary'] = '林悦：提供报价。'
        w.set_analysis(self.root, analysis, self.revision())
        for target in ('analysis.json', 'state.json', 'manifest.json', 'exports/latest.json'):
            with self.subTest(target=target):
                paths = [self.root / p for p in ('analysis.json', 'state.json', 'manifest.json', 'exports/latest.json')]
                before = {p: p.read_bytes() for p in paths}
                original_write = w.write
                def fail(path, value):
                    if Path(path) == self.root / target:
                        raise OSError('injected commit failure')
                    return original_write(path, value)
                with mock.patch.object(w, 'write', side_effect=fail), mock.patch.object(voiceprint, 'enroll_confirmed'):
                    with self.assertRaisesRegex(OSError, 'injected'):
                        w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_resubmitted_decision_with_old_name_stays_blocked(self):
        analysis = self.analysis()
        analysis['sources']['m']['reader']['summary'] = '林悦认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        analysis['sources']['m']['reader']['summary'] = '新概要。'
        analysis['sources']['m']['decisions'] = [{'text': '林悦认为报价可行。', 'status': 'open', 'evidence': [1]}]
        w.set_analysis(self.root, analysis, self.revision())
        self.assertNotIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])

    def test_old_name_in_fallback_core_point_cannot_clear_stale_marker(self):
        state = self.state()
        state['speakers'][0]['core_points'] = [{'text': '林悦认为报价可行。'}]
        w.write(self.root / 'state.json', state)
        analysis = self.analysis()
        analysis['sources']['m']['reader']['points'] = []
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        w.set_analysis(self.root, analysis, self.revision())
        self.assertNotIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])
        self.assertIn('林悦', self.state()['reader_stale_sources']['m']['remaining_old_names'])

    def test_rename_to_longer_name_still_detects_old_prefix_prose(self):
        analysis = self.analysis()
        analysis['sources']['m']['reader']['summary'] = '林悦认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '林悦明'}])), self.revision())
        self.assertNotIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])
        analysis['sources']['m']['reader']['summary'] = '林悦明认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        self.assertIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])

    def test_stale_marker_remembers_every_old_name_in_batch(self):
        analysis = self.analysis()
        analysis['sources']['m']['reader']['summary'] = '林悦：负责报价。陈思远认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}, {'key': 'm-B', 'name': '李四'}])), self.revision())
        analysis['sources']['m']['reader']['summary'] = '林悦：负责报价。李四认为报价可行。'
        w.set_analysis(self.root, analysis, self.revision())
        self.assertNotIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])
        self.assertIn('林悦', self.state()['reader_stale_sources']['m']['remaining_old_names'])

    def test_old_name_in_action_deadline_blocks_renamed_export(self):
        analysis = self.analysis()
        analysis['sources']['m']['reader']['todos'] = []
        analysis['sources']['m']['actions'][0]['deadline_text'] = '等林悦回复'
        w.set_analysis(self.root, analysis, self.revision())
        with mock.patch.object(voiceprint, 'enroll_confirmed'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '张三'}])), self.revision())
        self.assertNotIn('html', w.read(self.root / 'exports/latest.json')['files']['m'])
        self.assertIn('林悦', self.state()['reader_stale_sources']['m']['remaining_old_names'])

    def test_old_longer_name_is_not_hidden_by_new_shorter_name(self):
        self.assertEqual(w.remaining_old_names('林悦明认为报价可行。', ['林悦明'], ['林悦']), ['林悦明'])
        self.assertEqual(w.remaining_old_names('林悦明认为报价可行。', ['林悦'], ['林悦明']), [])

    def test_archive_rejects_direct_draft_and_forged_publication(self):
        latest = w.read(self.root / 'exports/latest.json')
        files = latest['files']['m']
        source = self.state()['sources'][0]
        audio = Path(source['audio_path'])
        before = audio.read_bytes()
        with self.assertRaisesRegex(ValueError, '--workspace'):
            archive_meeting.collect(audio, root=self.base / 'archive', html=files['html'], png=files['png'])
        w.write(self.root / 'publication.json', {'export_version': latest['version'], 'files': {}})
        with self.assertRaisesRegex(ValueError, '内容核对'):
            archive_meeting.collect_workspace(self.root, 'm', archive_root=self.base / 'archive')
        self.assertEqual(audio.read_bytes(), before)
        self.assertFalse((self.base / 'archive').exists())

    def test_user_resolution_refreshes_warning_and_binds_publishable_version(self):
        before = w.read(self.root / 'exports/latest.json')['version']
        resolutions = {p['id']: {'resolution': 'user_confirmed', 'note': '用户明确确认此处采用纪要表述'}
                       for p in audit.report(self.root)['issues']}
        self.assertTrue(resolutions)
        result = audit.resolve(self.root, resolutions, self.revision())
        latest = w.read(self.root / 'exports/latest.json')
        self.assertNotEqual(before, latest['version'])
        self.assertEqual(result['version'], latest['version'])
        parser = SeedParser(); parser.feed(Path(latest['files']['m']['html']).read_text())
        self.assertEqual(json.loads(parser.seed)['audit_issues'], [])
        self.assertTrue(audit.is_checked(self.root))
        w.publish(self.root)
        self.assertEqual(w.read(self.root / 'publication.json')['export_version'], latest['version'])

    def test_unresolved_audit_allows_html_png_but_publish_and_ai_approval_fail(self):
        self.pending()
        w.set_analysis(self.root, self.analysis(), self.revision())
        files = w.read(self.root / 'exports/latest.json')['files']['m']
        self.assertTrue(Path(files['html']).exists()); self.assertTrue(Path(files['png']).exists())
        parser = SeedParser(); parser.feed(Path(files['html']).read_text())
        issues = audit.report(self.root)['issues']
        self.assertTrue(issues); self.assertTrue(json.loads(parser.seed)['audit_issues'])
        with self.assertRaisesRegex(ValueError, '核对'):
            w.publish(self.root)
        analysis = self.analysis()
        analysis['sources']['m']['reader']['points'][1]['text'] = '林悦负责渠道。'
        w.set_analysis(self.root, analysis, self.revision())
        forbidden = next(p for p in audit.report(self.root)['issues'] if p['kind'] in ('name', 'negation', 'status'))
        with self.assertRaises(ValueError):
            audit.resolve(self.root, {forbidden['id']: {'resolution': 'supported', 'note': 'AI 认为没问题'}}, self.revision())
        self.assertFalse((self.root / 'publication.json').exists())


if __name__ == '__main__':
    unittest.main()
