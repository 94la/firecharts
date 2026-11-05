import time
import unittest
from unittest.mock import patch
from mtanalog.spread import measure, SpreadCache


class SpreadTests(unittest.TestCase):
    def quotes(self, url):
        if 'binance' in url:return {'symbol':'BTCUSDT','price':'100'}
        return {'retCode':0,'time':int(time.time()*1000),'result':{'list':[{'symbol':'BTCUSDT','lastPrice':'101'}]}}

    def test_current_difference_sign_and_percent(self):
        with patch('mtanalog.spread.read_json',side_effect=self.quotes):
            value=measure('BTCUSDT')
        self.assertEqual(value['difference'],1)
        self.assertAlmostEqual(value['percent'],1)
        self.assertTrue(value['available'])

    def test_missing_pair_and_stale_quotes_rejected(self):
        for result in ({'retCode':0,'result':{'list':[]}}, {'retCode':0,'time':1,'result':{'list':[{'symbol':'BTCUSDT','lastPrice':'101'}]}}):
            with patch('mtanalog.spread.read_json',side_effect=lambda url:self.quotes(url) if 'binance' in url else result):
                with self.assertRaises(ValueError):measure('BTCUSDT')

    def test_nonfinite_or_wrong_symbol_rejected(self):
        for price in ('nan','inf','0','-1'):
            with patch('mtanalog.spread.read_json',side_effect=lambda url:{'symbol':'BTCUSDT','price':price} if 'binance' in url else self.quotes(url)):
                with self.assertRaises(ValueError):measure('BTCUSDT')

    def test_cache_no_network_in_request_and_failure_removes_old_quote(self):
        cache=SpreadCache()
        try:
            with patch('mtanalog.spread.measure',side_effect=ValueError):
                cache.entries['BTCUSDT']=(time.monotonic(),{'available':True,'observed_ms':0})
                self.assertFalse(cache.get('BTCUSDT')['available'])
                cache.refresh('BTCUSDT')
                self.assertFalse(cache.get('BTCUSDT')['available'])
        finally:cache.close()
