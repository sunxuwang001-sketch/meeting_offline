"""The content check: minutes text against the words it cites, with synthetic meetings."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit
import device_context
import memory
import minutes_html
import workspace as w
from test_obsidian import fake_png

SAID = ['首发价我坚持 229 元，这个月不打折。', '要不再考虑一下渠道的事。', '毛利我十六号前算完，周五前给结论。', '没问题，就这么定。']


class FactTests(unittest.TestCase):
    def test_numbers_dates_and_weekdays_are_normalised(self):
        self.assertEqual(audit.cn_number('两万五'), 25000)
        self.assertLessEqual({('number', '229'), ('day', '16'), ('week', 'this-5')}, audit.facts(''.join(SAID)))
        self.assertEqual(audit.facts('三成预算，1.5万，百分之二十'), {('percent', '30'), ('number', '15000'), ('percent', '20')})
        self.assertEqual(audit.facts('一起看看三方的意见，万一不行再说'), set())
        evidence = audit.facts(SAID[2])
        self.assertTrue(audit.supported(('date', '10-16'), evidence))       # 十六号 backs “10 月 16 日”
        self.assertFalse(audit.supported(('week', 'next-5'), evidence))     # 周五 does not back 下周五
        self.assertTrue(audit.supported(('week', 'this-5'), audit.facts('这周五')))
        self.assertFalse(audit.negated('没问题，就这么定。'))
        self.assertTrue(audit.negated('这个月不打折。'))

    def test_each_kind_of_mismatch_is_caught_and_correct_paraphrase_passes(self):
        def kinds(claim, refs, **k):
            return sorted(p['kind'] for p in audit.check_claim('m', 'x', claim, refs, SAID, ['陈思远', '林悦'], **k))
        self.assertEqual(kinds('首发价定为 199 元', [0]), ['fact'])
        self.assertEqual(kinds('首发价 229 元，本月打折促销', [0]), ['negation'])
        self.assertEqual(kinds('渠道方案再议', [1], status='decided'), ['status'])
        self.assertEqual(kinds('渠道方案再议', [1], status='open'), [])
        self.assertEqual(kinds('陈思远负责渠道', [1]), ['name'])
        self.assertEqual(kinds('陈思远负责渠道', [1], allowed_names=['陈思远']), [])
        self.assertEqual(kinds('10 月 16 日前完成毛利测算，周五给结论', [2]), [])
        self.assertEqual(kinds('下周五给结论', [2]), ['fact'])
        self.assertEqual(kinds('方案已经定下来', [3], status='decided'), [])
        # ordinary meeting speech that is not a negation, and one that is
        for claim, said in [('下周给出报价', '能不能下周给？'), ('改为执行方案B', '方案A不行，换方案B'), ('不同渠道分开定价', '各个渠道分开定价')]:
            self.assertEqual(audit.check_claim('m', 'x', claim, [0], [said]), [], said)
        self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', '本季度招人', [0], ['这个季度不招。'])], ['negation'])
        self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', '方案通过', [0], ['我不同意，再考虑'], status='decided')], ['status'])
        self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', '预算 5 万', [0], ['预算一点五万'])], ['fact'])
        self.assertEqual(audit.facts('一百多万，五六十万，1万5，二零二六年'),
                         {('number', '1000000'), ('number', '500000'), ('number', '600000'), ('number', '15000'), ('year', '2026')})

    def test_explicit_times_preserve_period_and_24_hour_boundaries(self):
        equivalent = [
            ('上午三点', '03:00'), ('下午三点', '15:00'), ('3:00', '03:00'),
            ('下午3:30', '15:30'), ('早上三点半', '03:30'),
            ('中午十二点', '12:00'), ('中午一点', '13:00'),
            ('凌晨十二点', '00:00'), ('凌晨三点', '03:00'),
            ('晚上十二点', '00:00'), ('晚上九点', '21:00'),
            ('傍晚六点', '18:00'), ('上午十二点', '12:00'),
            ('十三点', '13:00'), ('二十四点', '24:00'),
            ('零点半', '00:30'), ('下午15:00', '15:00'),
        ]
        for spoken, clock in equivalent:
            with self.subTest(spoken=spoken):
                self.assertEqual(audit.facts(spoken), audit.facts(clock))
                self.assertEqual(audit.check_claim('m', 'x', clock, [0], [spoken]), [])
                self.assertEqual(audit.check_claim('m', 'x', spoken, [0], [clock]), [])
        different = [('上午三点', '下午三点'), ('03:00', '15:00'),
                     ('中午十二点', '凌晨十二点'), ('上午3:30', '下午3:30'),
                     ('24:00', '00:00'), ('下午三点', '上午三点')]
        for claim, said in different:
            with self.subTest(claim=claim, said=said):
                self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', claim, [0], [said])], ['fact'])

    def test_unspecified_spoken_time_cannot_establish_a_period_or_use_an_unrelated_number(self):
        self.assertEqual(audit.facts('三点半'), {('time_ambiguous', '3:30')})
        self.assertEqual(audit.facts('十二点半'), {('time_ambiguous', '12:30')})
        for claim, said in [('15:30', '三点半'), ('03:30', '三点半'),
                            ('12:30', '十二点半'), ('00:30', '十二点半'),
                            ('15:00', '三点交付'), ('03:00', '三点交付'),
                            ('15:00', '预算3元'), ('预算3元', '下午三点')]:
            with self.subTest(claim=claim, said=said):
                self.assertTrue(audit.check_claim('m', 'x', claim, [0], [said]))
        # Omitting the period does not invent a conflicting one. Bare 三点 may
        # mean three suggestions, so it stays numeric and can be explained manually.
        for said in ('15:30', '03:30', '三点半'):
            self.assertEqual(audit.check_claim('m', 'x', '三点半', [0], [said]), [])
        self.assertEqual(audit.facts('三点建议'), {('number', '3')})
        self.assertEqual(audit.facts('三点交付'), {('time_ambiguous', '3:00')})
        self.assertEqual(audit.check_claim('m', 'x', '三点交付', [0], ['下午三点交付']), [])
        self.assertEqual(audit.check_claim('m', 'x', '三点交付', [0], ['上午三点交付']), [])
        for hour, minute in [(25, 0), (24, 1), (3, 60), (-1, 0)]:
            self.assertIsNone(audit.time_fact(hour, minute))

    def test_issue_fingerprints_bind_all_corrected_evidence_not_unrelated_segments(self):
        cases = [('199元', '预算229元', {}, 'fact'),
                 ('陈思远负责', '讨论任务', {'names': ['陈思远']}, 'name'),
                 ('允许延期', '不允许延期', {}, 'negation'),
                 ('方案通过', '再考虑方案', {'status': 'decided'}, 'status')]
        for claim, said, options, kind in cases:
            with self.subTest(kind=kind):
                original = audit.check_claim('m', 'x', claim, [0], [said, '无关'], **options)[0]
                same = audit.check_claim('m', 'x', claim, [0], [said, '修改无关片段'], **options)[0]
                changed = audit.check_claim('m', 'x', claim, [0], [said + '。', '无关'], **options)[0]
                self.assertEqual(original['kind'], kind)
                self.assertEqual(original['id'], same['id'])
                self.assertNotEqual(original['id'], changed['id'])


class AuditFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'pc'})
        self.env.start()
        self.png = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png); self.png.start()
        self.store = mock.patch.object(memory, 'DEFAULT_STORE', device_context.paths()['memory']); self.store.start()
        transcript = self.base / 'm.json'
        w.write(transcript, {'model': 'synthetic', 'segments': [
            {'speaker_id': i % 2, 'start_ms': i * 4000, 'end_ms': i * 4000 + 3500, 'text': t} for i, t in enumerate(SAID)]})
        speakers = [{'key': f'm-{k}', 'source_id': 'm', 'source_speaker_id': i, 'role_name': n, 'display_name': n,
                     'identity_group': 'p-' + k, 'role_confirmed': True, 'utterances': []} for i, (k, n) in enumerate([('A', '林悦'), ('B', '陈思远')])]
        w.write(self.base / 's.json', {'sources': [{'source_id': 'm', 'path': str(transcript)}], 'speakers': speakers})
        audio = self.base / 'm.m4a'; audio.write_bytes(b'a')
        self.root = w.initialize(self.base / 's.json', self.base / 'meetings' / 'm', {'m': audio})

    def tearDown(self):
        self.store.stop(); self.png.stop(); self.env.stop(); self.tmp.cleanup()

    def analysis(self, price='199', status='decided'):
        return {'sources': {'m': {'meeting_date': '2026-10-12',
            'actions': [{'task': '完成毛利测算', 'owner': 'm-A', 'owner_basis': 'self_commitment', 'deadline_text': '10 月 16 日前', 'evidence': [2]}],
            'reader': {'topic': '定价会', 'summary': f'首发价 {price} 元，渠道再议。', 'summary_evidence': [0, 1],
                       'points': [{'title': '首发价', 'text': f'定为 {price} 元，这个月不打折。', 'evidence': [0], 'status': status},
                                  {'title': '渠道', 'text': '渠道方案再考虑。', 'evidence': [1], 'status': 'open'}],
                       'todos': [{'action_index': 0, 'deadline': '下周五'}]}}}}

    def revision(self):
        return w.read(self.root / 'state.json')['revision']

    def test_publishing_waits_until_every_mismatch_is_fixed_or_settled(self):
        w.set_analysis(self.root, self.analysis(), self.revision())
        found = audit.report(self.root)
        self.assertEqual(sorted((p['where'], p['kind']) for p in found['issues']),
                         [('actions[0].deadline', 'deadline'), ('points[0]', 'fact'), ('summary', 'fact')])
        with self.assertRaisesRegex(ValueError, '还没核对完：有 3 处'):
            w.publish(self.root)
        # fixing the text makes the amount problems disappear by themselves
        w.set_analysis(self.root, self.analysis(price='229'), self.revision())
        found = audit.report(self.root)
        self.assertEqual([p['kind'] for p in found['issues']], ['deadline'])
        with self.assertRaisesRegex(ValueError, 'note|依据'):
            audit.resolve(self.root, {found['issues'][0]['id']: {'resolution': 'user_confirmed'}}, self.revision())
        with self.assertRaisesRegex(ValueError, '不存在'):
            audit.resolve(self.root, {'nope': {'resolution': 'supported', 'note': 'x'}}, self.revision())
        record = audit.resolve(self.root, {found['issues'][0]['id']: {'resolution': 'user_confirmed', 'note': '用户确认是下周五'}}, self.revision())
        self.assertEqual(record['status'], 'checked')
        w.publish(self.root)
        # any later change to the minutes needs a fresh check; settled items carry over
        revision = self.revision()
        w.save_roles(self.root, [{'key': 'm-B', 'name': '陈思远', 'role_title': '财务', 'entity_type': 'person', 'scope': '测试'}], revision)
        with self.assertRaisesRegex(ValueError, '核对之后又改过'):
            w.publish(self.root)
        self.assertEqual(audit.resolve(self.root, {}, self.revision())['status'], 'checked')
        w.publish(self.root)
        # a confirmation belongs to the wording it was given for
        w.set_analysis(self.root, self.analysis(price='229'), self.revision())
        analysis = self.analysis(price='229'); analysis['sources']['m']['reader']['todos'] = [{'action_index': 0, 'deadline': '下下周五'}]
        w.set_analysis(self.root, analysis, self.revision())
        self.assertEqual(audit.report(self.root)['unresolved'], 1)

    def test_status_is_required_and_shown_in_the_minutes(self):
        analysis = self.analysis(price='229')
        del analysis['sources']['m']['reader']['points'][1]['status']
        analysis['sources']['m']['reader']['todos'] = []
        w.set_analysis(self.root, analysis, self.revision())
        self.assertEqual([(p['where'], p['kind']) for p in audit.report(self.root)['issues']], [('points[1]', 'status')])
        with self.assertRaisesRegex(ValueError, 'status'):
            w.set_analysis(self.root, {'sources': {'m': {'reader': {'points': [{'title': 't', 'text': 'x', 'evidence': [0], 'status': 'maybe'}], 'todos': []}}}}, self.revision())
        files = w.read(self.root / 'exports' / 'latest.json')['files']['m']
        self.assertIn('### 1. 首发价　`已决定`', Path(files['md']).read_text(encoding='utf-8'))
        self.assertIn('"status": "decided"', Path(files['html']).read_text(encoding='utf-8'))

    def test_fallback_viewpoints_cannot_publish_outside_reader_evidence_checks(self):
        state = w.read(self.root / 'state.json')
        state['speakers'][0]['core_points'] = ['产品报价 999 元。']
        w.write(self.root / 'state.json', state)
        for reader in (None, {}, {'points': []}):
            with self.subTest(reader=reader):
                section = {'title': '报价会'}
                if reader is not None:
                    section['reader'] = reader
                w.set_analysis(self.root, {'sources': {'m': section}}, self.revision())
                files = w.read(self.root / 'exports/latest.json')['files']['m']
                self.assertIn('999', Path(files['html']).read_text())  # still available as a draft preview
                with self.assertRaisesRegex(ValueError, 'reader.points'):
                    w.publish(self.root)
                self.assertFalse((self.root / 'publication.json').exists())
        # Once the visible prose has evidence, the existing deterministic checks apply to it.
        section['reader'] = {'points': [{'title': '价格', 'text': '首发价 999 元。', 'status': 'direction', 'evidence': [0]}]}
        w.set_analysis(self.root, {'sources': {'m': section}}, self.revision())
        self.assertGreater(audit.report(self.root)['unresolved'], 0)
        with self.assertRaisesRegex(ValueError, '还没核对完'):
            w.publish(self.root)
        section['reader']['points'][0]['text'] = '首发价 229 元。'
        w.set_analysis(self.root, {'sources': {'m': section}}, self.revision())
        self.assertEqual(audit.report(self.root)['unresolved'], 0)
        self.assertIn('m', w.publish(self.root)['files'])


    def test_pending_voice_name_cannot_be_attributed_to_another_speaker(self):
        state = w.read(self.root / 'state.json')
        state['speakers'][1].update(role_confirmed=False, identity_group=None, role_source='voice_match')
        w.write(self.root / 'state.json', state)
        analysis = {'sources': {'m': {'reader': {'points': [{'title': '分工', 'text': '陈思远负责渠道',
                    'evidence': [0], 'attribution': ['m-A'], 'status': 'open'}]}}}}
        w.set_analysis(self.root, analysis, self.revision())
        self.assertIn('name', [p['kind'] for p in audit.audit(self.root)])
        analysis['sources']['m']['reader']['points'][0]['attribution'] = ['m-B']
        w.set_analysis(self.root, analysis, self.revision())
        self.assertNotIn('name', [p['kind'] for p in audit.audit(self.root)])

    def set_claim(self, claim, said, status='open', deadline=False):
        transcript_path = self.base / 'm.json'
        transcript = w.read(transcript_path)
        transcript['segments'][0]['text'] = said
        w.write(transcript_path, transcript)
        self.claim_number = getattr(self, 'claim_number', 0) + 1
        self.root = w.initialize(self.base / 's.json', self.base / 'meetings' / f'claim-{self.claim_number}',
                                 {'m': self.base / 'm.m4a'})
        section = {'meeting_date': '2026-10-12', 'reader': {'points': [
            {'title': '议题', 'text': claim, 'evidence': [0], 'status': status}]}}
        if deadline:
            section['reader']['points'] = []
            section['actions'] = [{'task': '完成测算', 'owner': 'm-A', 'owner_basis': 'self_commitment',
                                   'deadline_text': claim, 'evidence': [0]}]
        w.set_analysis(self.root, {'sources': {'m': section}}, self.revision())
        return audit.report(self.root)['issues']

    def test_supported_fact_explanation_passes_real_detector_and_publish(self):
        cases = [
            ('年度预算12万元。', '每月预算一万元。', '每月1万元，全年12个月合计12万元。'),
            ('10月16日完成。', '周五前完成。', '会议日期为2026年10月12日，该周周五是10月16日。'),
            ('15:30开会。', '下午三点过半小时开会。', '下午三点加半小时为15:30。'),
            ('增长50%。', '从100增加到150。', '增加50，相对原来的100增长50%。'),
        ]
        for claim, said, note in cases:
            with self.subTest(claim=claim):
                issues = self.set_claim(claim, said)
                self.assertEqual([p['kind'] for p in issues], ['fact'])
                self.assertFalse(audit.is_checked(self.root))
                with self.assertRaisesRegex(ValueError, '依据'):
                    audit.resolve(self.root, {issues[0]['id']: {'resolution': 'supported', 'note': '  '}}, self.revision())
                result = audit.resolve(self.root, {issues[0]['id']: {'resolution': 'supported', 'note': note}}, self.revision())
                self.assertEqual(result['status'], 'checked')
                self.assertTrue(audit.is_checked(self.root))
                self.assertEqual(audit.report(self.root)['unresolved'], 0)
                w.publish(self.root)

    def test_supported_deadline_fact_passes_but_deadline_negation_does_not(self):
        issues = self.set_claim('10月16日前', '周五前完成测算。', deadline=True)
        self.assertEqual([p['kind'] for p in issues], ['deadline'])
        result = audit.resolve(self.root, {issues[0]['id']: {'resolution': 'supported',
                               'note': '本次会议当周的周五是10月16日。'}}, self.revision())
        self.assertEqual(result['status'], 'checked')
        w.publish(self.root)
        issues = self.set_claim('允许延期', '不允许延期。', deadline=True)
        self.assertEqual([p['kind'] for p in issues], ['negation'])
        self.assertEqual(issues[0]['where'], 'actions[0].deadline')
        with self.assertRaisesRegex(ValueError, 'user_confirmed'):
            audit.resolve(self.root, {issues[0]['id']: {'resolution': 'supported', 'note': 'AI认为可以延期'}}, self.revision())

    def test_corrected_evidence_invalidates_old_supported_but_unchanged_evidence_carries_over(self):
        issues = self.set_claim('预算199元。', '预算229元，其中税费30元。')
        key = issues[0]['id']
        audit.resolve(self.root, {key: {'resolution': 'supported', 'note': '229元减去30元税费为199元。'}}, self.revision())
        self.assertTrue(audit.is_checked(self.root))
        w.publish(self.root)
        # An unrelated export change does not discard an evidence-bound explanation.
        w.save_roles(self.root, [{'key': 'm-B', 'name': '陈思远', 'role_title': '财务',
                                'entity_type': 'person', 'scope': '测试'}], self.revision())
        self.assertEqual(audit.report(self.root)['issues'][0]['id'], key)
        self.assertEqual(audit.resolve(self.root, {}, self.revision())['status'], 'checked')
        # Normal supported correction path, same claim and same reference index.
        w.add_term(self.root, {'source_id': 'm', 'heard': '229', 'canonical': '329', 'scope': '测试'}, self.revision())
        latest = audit.report(self.root)
        self.assertEqual(latest['unresolved'], 1)
        self.assertNotEqual(latest['issues'][0]['id'], key)
        self.assertEqual(latest['issues'][0]['evidence_texts'], ['预算329元，其中税费30元。'])
        self.assertFalse(audit.is_checked(self.root))
        with self.assertRaisesRegex(ValueError, '不存在'):
            audit.resolve(self.root, {key: {'resolution': 'supported', 'note': '旧说明'}}, self.revision())
        record = audit.resolve(self.root, {}, self.revision())
        self.assertEqual(record['status'], 'pending')
        self.assertNotIn(key, record['resolutions'])
        with self.assertRaisesRegex(ValueError, '还没核对完'):
            w.publish(self.root)

    def test_am_pm_deadline_conflict_blocks_publication_until_explained(self):
        issues = self.set_claim('下午三点交付', '上午三点交付', deadline=True)
        self.assertEqual([p['kind'] for p in issues], ['deadline'])
        self.assertFalse(audit.is_checked(self.root))
        with self.assertRaisesRegex(ValueError, '还没核对完'):
            w.publish(self.root)
        # Keep the requested manual explanation policy; do not mechanically judge it.
        record = audit.resolve(self.root, {issues[0]['id']: {'resolution': 'supported',
                               'note': '已复核上下文，原话时段口误，采用下午交付。'}}, self.revision())
        self.assertEqual(record['status'], 'checked')

    def test_legacy_fact_supported_remains_checked_with_explanation(self):
        issues = self.set_claim('年度预算12万元。', '每月预算一万元。')
        self.assertEqual([p['kind'] for p in issues], ['fact'])
        latest = w.read(self.root / 'exports/latest.json')
        w.write(self.root / 'semantic_review.json', {'version': latest['version'], 'resolutions': {
            issues[0]['id']: {'resolution': 'supported', 'note': '每月1万元，全年合计12万元。'}}})
        self.assertTrue(audit.is_checked(self.root))
        self.assertEqual(audit.report(self.root)['unresolved'], 0)
        self.assertEqual(audit.resolve(self.root, {}, self.revision())['status'], 'checked')
        self.assertTrue(audit.is_checked(self.root))
        w.publish(self.root)

    def test_semantic_supported_is_rejected_and_legacy_resolution_needs_user_confirmation(self):
        cases = [('name', '陈思远负责渠道。', '讨论渠道方案。', 'open'),
                 ('negation', '本月打折。', '本月不打折。', 'open'),
                 ('status', '方案通过。', '再考虑一下方案。', 'decided')]
        for kind, claim, said, status in cases:
            with self.subTest(kind=kind):
                issues = self.set_claim(claim, said, status)
                self.assertEqual([p['kind'] for p in issues], [kind])
                key = issues[0]['id']
                legacy = {key: {'resolution': 'supported', 'note': '原来的助手认为原话足以支持。'}}
                with self.assertRaisesRegex(ValueError, 'user_confirmed'):
                    audit.resolve(self.root, legacy, self.revision())
                latest = w.read(self.root / 'exports/latest.json')
                w.write(self.root / 'semantic_review.json', {'version': latest['version'], 'resolutions': legacy})
                self.assertFalse(audit.is_checked(self.root))
                self.assertFalse(audit.report(self.root)['checked'])
                self.assertEqual(audit.report(self.root)['unresolved'], 1)
                with self.assertRaisesRegex(ValueError, '还没核对完'):
                    w.publish(self.root)
                result = audit.resolve(self.root, {}, self.revision())
                self.assertEqual(result['status'], 'pending')
                self.assertNotIn(key, result['resolutions'])
                confirmed = {key: {'resolution': 'user_confirmed', 'note': '用户明确确认此处采用纪要表述。'}}
                self.assertEqual(audit.resolve(self.root, confirmed, self.revision())['status'], 'checked')
                self.assertTrue(audit.is_checked(self.root))
                w.publish(self.root)


class ResolutionTypeTests(unittest.TestCase):
    def test_supported_checks_only_issue_type_and_nonempty_explanation(self):
        for kind in ('fact', 'deadline'):
            with self.subTest(kind=kind):
                self.assertTrue(audit.resolution_valid({'kind': kind},
                    {'resolution': 'supported', 'note': '原话有依据，已说明换算关系。'}))
                for note in ('', '  ', None, 0, [], {}):
                    self.assertFalse(audit.resolution_valid({'kind': kind},
                        {'resolution': 'supported', 'note': note}))
                self.assertFalse(audit.resolution_valid({'kind': kind}, {'resolution': 'supported'}))

    def test_semantic_types_require_user_confirmation(self):
        for kind in ('name', 'negation', 'status', 'other'):
            with self.subTest(kind=kind):
                self.assertFalse(audit.resolution_valid({'kind': kind},
                    {'resolution': 'supported', 'note': 'AI认为没问题'}))
                self.assertTrue(audit.resolution_valid({'kind': kind}, {'resolution': 'user_confirmed'}))
        for resolution in (None, 'supported', {}, {'resolution': 'fixed'}):
            self.assertFalse(audit.resolution_valid({'kind': 'fact'}, resolution))


if __name__ == '__main__':
    unittest.main()
