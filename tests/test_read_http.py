import unittest
from unittest.mock import Mock, patch
import httpx

from facturador.read_http import read_with_retry, ServiceWait


class ReadRetryTests(unittest.TestCase):
    @staticmethod
    def error(status, delay=''):
        req=httpx.Request('GET','https://ejemplo.invalid')
        response=httpx.Response(status, request=req, headers={'Retry-After':delay})
        return httpx.HTTPStatusError('fallo',request=req,response=response)

    @patch('facturador.read_http.time.sleep')
    def test_read_waits_for_retry_after_and_recovers(self, sleep):
        call=Mock(side_effect=[self.error(429,'3'), httpx.ConnectTimeout('sin red'), 'listo'])
        self.assertEqual(read_with_retry(call),'listo')
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [3,2])

    @patch('facturador.read_http.time.sleep')
    def test_long_wait_and_denied_lookup_do_not_retry(self, sleep):
        for status, error in [(503, ServiceWait), (403,httpx.HTTPStatusError)]:
            call=Mock(side_effect=self.error(status,'90'))
            with self.assertRaises(error):
                read_with_retry(call)
            call.assert_called_once()
        sleep.assert_not_called()
