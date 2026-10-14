import unittest
from athena.search_eval import run


class SearchQualityTests(unittest.TestCase):
    def test_lexical_baseline_and_documented_semantic_gaps(self):
        report = run()
        for row in report['results']:
            if row['category'] != 'paraphrase':
                self.assertTrue(row['passed'], row['query'])
        self.assertEqual(report['negative_accuracy'], 1)
        self.assertGreater(report['hit_at_k'], .8)
        self.assertTrue(all(not row['passed'] for row in report['results']
                            if row['category'] == 'paraphrase'))
