"""All role confirmation routes synchronize prose or retain the stale-publication gate."""
import contextlib
import io
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit
import review
import workspace as w
import test_audit
from test_minutes_html import SeedParser


class RoleSyncRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_audit.AuditFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def seed(self, prose='林悦：首发价 229 元，这个月不打折。'):
        analysis = self.fixture.analysis(price='229')
        reader = analysis['sources']['m']['reader']
        reader['todos'] = []
        reader.update(summary=prose, summary_evidence=[0])
        reader['points'][0].update(text=prose, attribution=['m-A'])
        w.set_analysis(self.root, analysis, self.fixture.revision())

    def mappings(self):
        return [{'key': 'm-A', 'name': '张三', 'entity_type': 'person', 'scope': '测试'}]

    def assert_synced(self, exports):
        parser = SeedParser()
        parser.feed(Path(exports['files']['m']['html']).read_text())
        doc = json.loads(parser.seed)
        self.assertEqual(doc['participants'], '张三、陈思远')
        self.assertTrue(doc['summary'].startswith('张三：'))
        self.assertTrue(doc['points'][0]['text'].startswith('张三：'))
        self.assertEqual(doc['todos'][0]['owner'], '张三')
        self.assertNotIn('m', w.read(self.root / 'state.json').get('reader_stale_sources', {}))
        self.assertEqual(audit.report(self.root)['unresolved'], 0)
        self.assertIn('m', w.publish(self.root)['files'])

    def test_review_http_roles_synchronizes_summary_points_and_owner(self):
        self.seed()
        server = review.ReviewServer(self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({'revision': self.fixture.revision(), 'mappings': self.mappings()}).encode()
            request = urllib.request.Request(server.origin + '/api/roles', data=body, method='POST', headers={
                'X-Meeting-Token': server.token, 'Content-Type': 'application/json', 'Origin': server.origin})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                result = json.load(response)
            self.assertTrue(result['saved'])
            self.assert_synced(result['exports'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_roles_cli_synchronizes_prose(self):
        self.seed()
        path = self.root / 'role-mapping.json'
        w.write(path, self.mappings())
        argv = ['workspace.py', 'roles', '--workspace', str(self.root), '--file', str(path),
                '--revision', str(self.fixture.revision())]
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            w.main()
        self.assert_synced(w.read(self.root / 'exports/latest.json'))

    def test_unsafe_prose_stays_blocked_after_unchanged_analysis(self):
        self.seed('林悦认为首发价 229 元，这个月不打折。')
        w.save_roles(self.root, self.mappings(), self.fixture.revision())
        self.assertIn('m', w.read(self.root / 'state.json')['reader_stale_sources'])
        unchanged = w.read(self.root / 'analysis.json')
        w.set_analysis(self.root, unchanged, self.fixture.revision())
        self.assertIn('m', w.read(self.root / 'state.json')['reader_stale_sources'])
        with self.assertRaisesRegex(ValueError, '旧名字|正文|还没有可发布'):
            w.publish(self.root)
        section = unchanged['sources']['m']['reader']
        section['summary'] = section['summary'].replace('林悦', '张三')
        section['points'][0]['text'] = section['points'][0]['text'].replace('林悦', '张三')
        w.set_analysis(self.root, unchanged, self.fixture.revision())
        self.assertNotIn('m', w.read(self.root / 'state.json')['reader_stale_sources'])

    def test_keeping_same_identity_leaves_prose_unchanged(self):
        self.seed()
        before = w.read(self.root / 'analysis.json')
        w.save_roles(self.root, [{'key': 'm-A', 'name': '林悦', 'scope': '测试'}], self.fixture.revision())
        self.assertEqual(w.read(self.root / 'analysis.json'), before)
        self.assertNotIn('m', w.read(self.root / 'state.json').get('reader_stale_sources', {}))

    def test_withdrawing_identity_removes_old_named_prose(self):
        self.seed()
        w.save_roles(self.root, [{'key': 'm-A', 'keep_unknown': True}], self.fixture.revision())
        section = w.read(self.root / 'analysis.json')['sources']['m']
        self.assertNotIn('林悦', w.analysis_prose(section))
        self.assertTrue(w.read(self.root / 'state.json')['speakers'][0]['accepted_unknown'])


if __name__ == '__main__':
    unittest.main()
