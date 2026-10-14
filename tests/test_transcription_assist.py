import tests.isolation  # noqa: F401: prevents private-library/network access
import unittest

from qwen_asr_app.transcription_assist.validation import correction_risks, protect_correction
from qwen_asr_app.transcription_assist.service import select_terms


class EvidenceAssistanceTests(unittest.TestCase):
    def test_textbook_number_cannot_replace_teacher_number(self):
        source={'id':7,'raw':'本次样品的NA是0.22，不是课件中的0.20。'}
        safe,candidate=protect_correction(source,'本次样品的NA是0.20，不是课件中的0.20。',[{'text':'NA=0.20'}])
        self.assertEqual(safe,source['raw'])
        self.assertEqual(candidate['status'],'待核对')
        self.assertIn('数字或单位变化',candidate['risks'])

    def test_units_negation_and_conditions_are_protected(self):
        for before,after in [('功率为40 mW','功率为40 W'),('相干解调不会产生门限效应','相干解调会产生门限效应'),('条件 x ≤ 0.22','条件 x ≥ 0.22')]:
            # Include generic 不 as a negation as well as phrase forms.
            if '不会' in before:
                before=before.replace('不会','不能');after=after.replace('会产生','产生')
            self.assertTrue(correction_risks(before,after),(before,after))

    def test_punctuation_edit_and_standard_term_keep_values(self):
        self.assertEqual(correction_risks('数值孔经0.22，功率40 mW。','数值孔径0.22；功率40 mW。'),[])

    def test_dynamic_terms_rank_evidence_without_adding_facts(self):
        static=['信源','信道','包络检波','门限效应','SNR（信噪比）']
        ev=[{'text':'包络检波的小信噪比会出现门限效应','citation_id':'K123'}]
        terms,reasons=select_terms(static,'这里讲门限效应',ev,limit=3)
        self.assertEqual(terms[0],'门限效应')
        self.assertIn('包络检波',terms)
        self.assertEqual(len(reasons),3)


if __name__=='__main__':
    unittest.main()
