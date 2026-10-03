import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nightshift.policy import Policy
from nightshift.providers.base import ProviderRequest, ProviderResult
from nightshift.providers.grok import GrokProvider


class GrokCompletionTests(unittest.TestCase):
    def test_cancelled_or_truncated_stream_cannot_count_as_provider_success(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = ProviderRequest('ns-test', root, root / 'prompt', root / 'stdout', root / 'stderr',
                                      {}, 10, 'workspace', 'session', None, 10)
            for event in ({'type': 'end', 'stopReason': 'cancelled'},
                          {'type': 'end', 'stopReason': 'max_turns'}, {'type': 'thought', 'data': 'unfinished'}):
                request.stdout_path.write_text(json.dumps(event) + '\n')
                with self.subTest(event=event), patch.dict('os.environ', {'NIGHTSHIFT_FORBID_GROK': '0'}), patch('nightshift.providers.grok.run_subprocess',
                                                     return_value=ProviderResult(exit_code=0)):
                    result = GrokProvider('/usr/bin/true').execute(request, policy=Policy('dontAsk', 'workspace', (), (), True, ''))
                    self.assertNotEqual(result.exit_code, 0)
                    self.assertIn('completion', result.failure_reason)

    def test_completed_stream_preserves_zero_exit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = ProviderRequest('ns-test', root, root / 'prompt', root / 'stdout', root / 'stderr',
                                      {}, 10, 'workspace', 'session', None, 10)
            request.stdout_path.write_text('{"type":"end","stopReason":"completed"}\n')
            with patch.dict('os.environ', {'NIGHTSHIFT_FORBID_GROK': '0'}), patch('nightshift.providers.grok.run_subprocess', return_value=ProviderResult(exit_code=0)):
                result = GrokProvider('/usr/bin/true').execute(request, policy=Policy('dontAsk', 'workspace', (), (), True, ''))
                self.assertEqual(result.exit_code, 0)
