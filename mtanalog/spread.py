"""Bounded, ephemeral Spot price comparison. Never writes market history."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
import threading
import time
from urllib.request import urlopen
from urllib.parse import urlencode


def read_json(url):
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def measure(symbol):
    if not re.fullmatch(r'[A-Z0-9]{2,32}', symbol):
        raise ValueError('Invalid symbol')
    start = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        binance = pool.submit(read_json, 'https://data-api.binance.vision/api/v3/ticker/price?'+urlencode({'symbol':symbol}))
        bybit = pool.submit(read_json, 'https://api.bybit.com/v5/market/tickers?'+urlencode({'category':'spot','symbol':symbol}))
        b, y = binance.result(), bybit.result()
    end = time.time()
    if end-start > 5 or end < start:
        raise ValueError('Quotes not sufficiently synchronized')
    rows = y.get('result', {}).get('list', [])
    if b.get('symbol') != symbol or y.get('retCode') != 0 or len(rows) != 1 or rows[0].get('symbol') != symbol:
        raise ValueError('Spot pair unavailable')
    # Bybit response time guards against a stale upstream cache.
    if abs(end*1000-float(y.get('time',0))) > 10000:
        raise ValueError('Stale quote')
    bp, yp = float(b['price']), float(rows[0]['lastPrice'])
    if not all(math.isfinite(p) and p > 0 for p in (bp,yp)):
        raise ValueError('Invalid quote')
    return dict(available=True, symbol=symbol, observed_ms=int(end*1000),
                binance=bp, bybit=yp, difference=yp-bp, percent=(yp/bp-1)*100,
                source='spot-last-price', sampling_ms=round((end-start)*1000))


class SpreadCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='spot-spread')
        self.entries = {}
        self.pending = set()

    def get(self, symbol):
        now = time.monotonic()
        with self.lock:
            stamp, value = self.entries.get(symbol, (0, {'available':False, 'reason':'loading'}))
            if now-stamp >= 15 and symbol not in self.pending and len(self.pending) < 6:
                self.pending.add(symbol)
                self.pool.submit(self.refresh, symbol)
            result = dict(value)
        if result.get('available') and time.time()*1000-result['observed_ms'] > 45000:
            return {'available':False, 'reason':'stale'}
        return result

    def refresh(self, symbol):
        try:
            value = measure(symbol)
        except Exception:
            value = {'available':False, 'reason':'unavailable'}
        with self.lock:
            self.entries[symbol] = (time.monotonic(), value)
            self.pending.discard(symbol)
            while len(self.entries) > 32:
                self.entries.pop(next(iter(self.entries)))

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
