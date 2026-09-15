"""Exercise quota protocol boundaries without a Codex process or account."""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import meter


class QuotaRpcTests(unittest.TestCase):
    def transport(self, replies):
        factory = mock.MagicMock()
        rpc = factory.return_value.__enter__.return_value
        rpc.responses.return_value = iter(replies)
        return factory, rpc

    def test_only_initialized_quota_response_is_accepted(self):
        factory, rpc = self.transport([
            {'id': 2, 'result': {'rateLimits': {'primary': {'usedPercent': 90}}}},
            {'id': True, 'result': {}},
            {'id': 1, 'result': {}},
            {'id': 1, 'result': {}},
            {'method': 'unrelated', 'params': {'private': 'discarded'}},
            {'id': 2, 'result': {'rateLimits': {'primary': {'usedPercent': 12.5}}}},
        ])
        with mock.patch('rpc_transport.JsonRpcProcess', factory), mock.patch.object(
                meter, 'codex_command', return_value=['node.exe', 'codex.js']):
            result = meter.fetch_quota()
        self.assertEqual(result['buckets'][0]['windows'][0]['remainingPercent'], 87.5)
        factory.assert_called_once_with(['node.exe', 'codex.js', 'app-server', '--stdio'], timeout=20)
        self.assertEqual([call.args[0]['method'] for call in rpc.send.call_args_list],
                         ['initialize', 'initialized', 'account/rateLimits/read'])
        factory.return_value.__exit__.assert_called_once()

    def test_rpc_diagnostics_are_not_exposed(self):
        for replies in ([{'id': 1, 'error': {'message': 'SYNTHETIC_PRIVATE_DETAIL'}}],
                        [{'id': 1, 'result': {}},
                         {'id': 2, 'error': {'message': 'SYNTHETIC_PRIVATE_DETAIL'}}]):
            factory, rpc = self.transport(replies)
            with self.subTest(replies=replies), mock.patch('rpc_transport.JsonRpcProcess', factory), \
                    mock.patch.object(meter, 'codex_command', return_value=['codex']), \
                    self.assertRaises(RuntimeError) as caught:
                meter.fetch_quota()
            self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', str(caught.exception))
            factory.return_value.__exit__.assert_called_once()

    def test_timeout_and_eof_are_unavailable(self):
        for failure in (None, TimeoutError('SYNTHETIC_PRIVATE_DETAIL')):
            factory, rpc = self.transport([])
            if failure:
                rpc.responses.side_effect = failure
            with self.subTest(failure=failure), mock.patch('rpc_transport.JsonRpcProcess', factory), \
                    mock.patch.object(meter, 'codex_command', return_value=['codex']), \
                    self.assertRaises(RuntimeError) as caught:
                meter.fetch_quota()
            self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', str(caught.exception))
            factory.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
