import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock,patch
from test_instagram_event_extraction import serial
from app.ai_analyze_service import gemini_analysis as ga
import run_instagram_best_effort_analysis as runner

class AnalysisTests(unittest.TestCase):
    def test_failed_post_continues_and_non_event_is_persisted(self):
        row=SimpleNamespace(ai_analyzed=False,post_shortcode='sample')
        db=Mock();db.scalar.return_value=row
        session=Mock();session.__enter__=Mock(return_value=db);session.__exit__=Mock(return_value=False)
        non_event=SimpleNamespace(is_event=False,events=[])
        event=SimpleNamespace(is_event=True,events=[{}])
        with patch('Blueprint_db.SessionLocal',return_value=session), \
             patch('google.genai.Client'),patch.object(ga,'gemini_api_key',return_value='test'), \
             patch('app.parent_pipeline.service.parent_lock',side_effect=lambda pid:nullcontext(True)), \
             patch('app.parent_pipeline.daily_budget.Ledger') as ledger, \
             patch('app.parent_pipeline.attempt_safety.review_required',return_value=False), \
             patch('app.parent_pipeline.attempt_safety.Attempt'), \
             patch('app.ai_analyze_service.ai_analyze.build_analyzer_post_dict',return_value={'shortcode':'sample'}), \
             patch.object(ga,'_image_urls_from_post',return_value=['https://example.test/a.jpg']), \
             patch.object(ga,'analyze_post',side_effect=[ValueError('private'),(non_event,0),(event,0)]), \
             patch.object(ga,'ai_to_db_update_values',side_effect=lambda ai,*a,**kw:{'is_event':ai.is_event,'ai_analyzed':True}), \
             patch.object(ga,'ai_to_child_event_values',side_effect=lambda ai,*a:ai.events), \
             patch('instagram_event_reconciliation.persist_analysis',return_value=True) as persist:
            ledger.return_value.summary.return_value={}
            result=runner.execute('sample',[1,2,3])
        self.assertEqual(result['failed'],1);self.assertEqual(result['succeeded'],2)
        self.assertEqual(persist.call_count,2)
        self.assertFalse(persist.call_args_list[0].args[3]['is_event'])
        self.assertEqual(persist.call_args_list[0].args[4],[])
        self.assertEqual(result['posts'][2]['child_events'],1)
        self.assertNotIn('private',str(result))
