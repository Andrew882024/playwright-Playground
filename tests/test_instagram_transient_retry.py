import unittest
from unittest.mock import patch
import run_instagram_best_effort_analysis as runner


def outcome(status=503, success=False):
    item=dict(parent_id=2127,status='succeeded' if success else 'failed',
              gemini_http_requests=1,phase='gemini_request',http_status=None if success else status)
    return dict(status='completed' if success else 'partial',posts=[item],gemini_http_requests=1,daily_budget={})


class RetryTests(unittest.TestCase):
    def invoke(self,responses,ids=(2127,)):
        with patch.object(runner,'_execute_once',side_effect=responses) as call, \
             patch('app.parent_pipeline.attempt_safety.inspect',return_value={'review_required':True,'last_request_state':'failed'}), \
             patch('time.sleep') as sleep:
            result=runner.execute('sixthcoboard',ids,retry_transient=True)
        return result,call,sleep

    def test_backoff_and_three_total_attempts(self):
        result,call,sleep=self.invoke([outcome(),outcome(504),outcome(success=True)])
        self.assertEqual(call.call_count,3);self.assertEqual(result['gemini_http_requests'],3)
        self.assertEqual(result['succeeded'],1)
        self.assertEqual([x.args[0] for x in sleep.call_args_list],[2,4])
        self.assertTrue(all(x.kwargs['authorize_retry'] for x in call.call_args_list))

    def test_exhausted_post_continues_to_second(self):
        result,call,sleep=self.invoke([outcome()]*3+[outcome(success=True)],(2127,2128))
        self.assertEqual(result['failed'],1);self.assertEqual(result['succeeded'],1)
        self.assertEqual(result['gemini_http_requests'],4)

    def test_only_allowlisted_http_errors_retry(self):
        for code in (400,401,403,404,None):
            result,call,sleep=self.invoke([outcome(code)])
            self.assertEqual(call.call_count,1);sleep.assert_not_called()
        for code in (429,500,502,503,504):
            result,call,sleep=self.invoke([outcome(code),outcome(success=True)])
            self.assertEqual(call.call_count,2)

    def test_unknown_transport_outcome_is_not_retried(self):
        with patch('app.parent_pipeline.attempt_safety.inspect',return_value={'review_required':True,'last_request_state':'uncertain'}),patch.object(runner,'_execute_once') as call:
            result=runner.execute('sixthcoboard',[2127],retry_transient=True)
        call.assert_not_called();self.assertEqual(result['skipped'],1)
