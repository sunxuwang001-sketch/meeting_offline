"""No browser/model dependency: inspect UI wiring, optionally run pure JS guard."""
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class VoiceprintReviewUI(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / 'assets/review.html').read_text(encoding='utf-8')

    def test_pending_match_is_labelled_and_score_is_not_probability(self):
        self.assertIn("speaker.role_source==='voice_match'", self.html)
        self.assertIn('声纹自动识别 · 待确认', self.html)
        self.assertIn('voice_similarity', self.html)
        self.assertIn('相似度不是身份确认，也不是正确概率', self.html)
        self.assertIn("consent.checked=false", self.html)

    def test_save_skips_untouched_pending_identities(self):
        self.assertIn('if(!shouldSaveRole(card))continue;', self.html)
        self.assertIn("consent.className='confirm-identity'", self.html)
        # No preselection, timeout, or render event counts as consent.
        self.assertNotIn('consent.checked=true', self.html)
        self.assertNotIn("if(!speaker.role_confirmed){const confirmation", self.html)

    @unittest.skipUnless(shutil.which('node'), 'optional Node is unavailable')
    def test_pure_save_guard_executes(self):
        function = re.search(r'function shouldSaveRole\(card\)\{[^\n]+\}', self.html).group(0)
        js = function + '''
const assert=require('assert');
const card=(tick,was='false',old='a',value='a')=>({dataset:{wasConfirmed:was,originalRole:old},querySelector:q=>q==='.role-select'?{value}:tick});
assert.strictEqual(shouldSaveRole(card({checked:false})),false);
assert.strictEqual(shouldSaveRole(card({checked:true})),true);
assert.strictEqual(shouldSaveRole(card(null)),false);
assert.strictEqual(shouldSaveRole(card({checked:'true'})),false);
assert.strictEqual(shouldSaveRole(card({checked:false},'true')),false);
assert.strictEqual(shouldSaveRole(card({checked:false},'true','a','b')),true);
assert.strictEqual(shouldSaveRole(card({checked:false},'false','a','b')),false);
'''
        subprocess.run([shutil.which('node'), '-e', js], check=True, capture_output=True)

    def test_guidance_keeps_sandbox_and_zero_match_exception(self):
        for filename in ('SKILL.md', 'references/roles.md', 'references/local-review.md'):
            text = (ROOT / filename).read_text(encoding='utf-8')
            self.assertIn('云端沙箱', text)
            self.assertIn('没有认出任何人', text)
            self.assertIn('import-html', text)
        skill = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('身份只由用户确认', skill)
        self.assertIn('不能替用户登记确认', skill)
        self.assertIn('长期库只存用户确认的内容', skill)


if __name__ == '__main__':
    unittest.main()
