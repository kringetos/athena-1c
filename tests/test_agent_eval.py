import copy
import unittest
from athena.agent_eval import score


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.case={'expected':['Calc'],'facts':['Amount','Price']}
        self.symbols=[{'id':'symbol','name':'Calc','file_id':'file','path':'Module.bsl','start_line':1,'end_line':3,
                       'code':'Procedure Calc()\nAmount = Price * Qty;\nEndProcedure'}]
        self.job={'status':'ready','answer':'Calc Module.bsl:2 Amount = Price * Qty;',
                  'steps':[],'coverage':{'read_ranges':[{'file_id':'file','start_line':1,'end_line':3}]}}

    def test_named_answer_without_delivered_evidence_fails(self):
        job=copy.deepcopy(self.job);job['coverage']['read_ranges']=[]
        self.assertFalse(score(self.case,job,self.symbols)['passed'])

    def test_header_only_does_not_support_body_claims(self):
        job=copy.deepcopy(self.job);job['coverage']['read_ranges'][0]['end_line']=1
        result=score(self.case,job,self.symbols)
        self.assertFalse(result['passed']);self.assertEqual(result['source_supported_markers'],[])

    def test_answer_markers_require_code_and_source_location(self):
        self.assertTrue(score(self.case,self.job,self.symbols)['passed'])
        self.job['answer']='Calc: Amount = Price * Qty;'
        self.assertFalse(score(self.case,self.job,self.symbols)['passed'])
