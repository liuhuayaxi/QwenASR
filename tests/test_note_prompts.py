from tests import isolation  # noqa: F401
import unittest
from qwen_asr_app import note_prompts as np


class NotePromptTests(unittest.TestCase):
    def test_modules_follow_lesson_features(self):
        scope = {'enabled': True, 'items': [{'level': 'mastery', 'text': '掌握P-I特性'}]}
        package = {'sources': [{'name': '翻转课堂第二次课课件.pdf'}]}
        found = np.features(package, scope, [{'id': 'q'}])
        self.assertEqual(found, {'learning_requirements': True, 'yuketang': True, 'preview': True})
        text = np.compose(found)
        self.assertIn('## 本章学习要求', text); self.assertIn('预习', text); self.assertIn('## 课上习题', text)
        plain = np.compose(np.features({'sources': [{'name': '第02章.pdf'}]}, {}, []))
        self.assertNotIn('## 本章学习要求', plain); self.assertNotIn('课上习题', plain); self.assertIn('章节标题', plain)

    def test_clean_removes_meta_ascii_diagrams_and_internal_comments(self):
        md = ('> 新旧内容判断范围：本节完整转写。不能证明老师此前没有讲过。\n\n## P-I 特性\n\n'
              '<!-- knowledge:p1 -->\n正文\n\n```\nP\n|        /\n|       /\n|______/____ I\n```\n\n'
              '```python\nprint(1)\n```\n\n<!-- yuketang:Y1 -->\n')
        out, report = np.clean(md)
        self.assertNotIn('新旧内容', out); self.assertNotIn('knowledge:', out); self.assertNotIn('|______', out)
        self.assertIn('print(1)', out); self.assertIn('<!-- yuketang:Y1 -->', out)
        self.assertEqual(report['ascii_blocks_removed'], 1)

    def test_bold_and_date(self):
        self.assertEqual(np.bold_cjk_safe('受**俄歇复合（Auger）**几率'), '受<strong>俄歇复合（Auger）</strong>几率')
        self.assertEqual(np.bold_cjk_safe('**定义**：x'), '**定义**：x')
        self.assertEqual(np.human_date('2026-10-14T08:00:00+08:00'), '2026年10月14日 08:00')


    def test_exercise_section_layout_is_normalized(self):
        from qwen_asr_app.yuketang.retrieval import normalize_section
        md = '## 课上习题\n### 第1题\n<!-- yuketang:Y1 -->\n题干\nA. m>1\nB. m<1\n**答案**：B\n**解析**：因为 $m<1$\n\n## 其他\n正文'
        out = normalize_section(md)
        self.assertIn('题干\n\n- A. m>1\n- B. m<1\n\n**答案**：B\n\n**解析**', out)
        self.assertTrue(out.endswith('## 其他\n正文'))
        self.assertEqual(normalize_section(out), out)
        self.assertEqual(np.bold_cjk_safe('受**效率（$\\eta$）**定义'), '受<strong>效率（$\\eta$）</strong>定义')


if __name__ == '__main__':
    unittest.main()
