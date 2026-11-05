import json
import fcntl
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from mtanalog.private_access import create_link,valid_token,authorized,COOKIE
from mtanalog.private_pairs import PairManager,symbol_name
from mtanalog.core import Book,SequenceGap,MinuteMap,trade_values
from mtanalog.storage import Store
from mtanalog.web import ChartService,handler_for
from mtanalog.webdata import chart_data,price_settings,grouped_bins

CATALOG={'symbols':[{'symbol':'ETHBTC','baseAsset':'ETH','quoteAsset':'BTC','status':'TRADING','isSpotTradingAllowed':True,'filters':[{'filterType':'PRICE_FILTER','tickSize':'0.00000100'}]},
                    {'symbol':'BADBTC','baseAsset':'BAD','quoteAsset':'BTC','status':'BREAK','isSpotTradingAllowed':True,'filters':[]}]}


def config(root):
 return dict(data_dir=root,price_step=200,base_price_step=25,min_liquidity_usdt=5e6,rest_url='https://example.invalid',min_free_gib=0)


class PrecisionTests(unittest.TestCase):
 def test_quote_cvd_keeps_one_satoshi_execution(self):
  event=dict(p='0.01',q='0.000001',m=False)
  self.assertEqual(trade_values(event,100_000_000),(0,1))
  event['m']=True
  self.assertEqual(trade_values(event,100_000_000),(0,-1))

 def test_small_price_levels_never_collapse_to_cents(self):
  book=Book(.00000001,100_000_000)
  payload=dict(lastUpdateId=1,bids=[['0.00000011','10000'],['0.00000010','20000']],asks=[['0.00000012','10000']])
  book.snapshot(payload)
  self.assertEqual(set(book.bids),{10,11});self.assertAlmostEqual(sum(book.bins.values()),.0043)
  with self.assertRaises(SequenceGap):book.refreshed({**payload,'lastUpdateId':2},[])

 def test_refresh_and_snapshot_round_trip_at_eight_decimals(self):
  book=Book(.00000001,100_000_000)
  payload=dict(lastUpdateId=1,bids=[['0.00000011','10000']],asks=[['0.00000012','10000']])
  book.snapshot(payload);book.apply(dict(U=2,u=2,b=[['0.00000010','20000']],a=[]))
  candidate=book.refreshed(payload,[dict(U=2,u=2,b=[['0.00000010','20000']],a=[])])
  book.apply_refresh(book.refresh_record(candidate))
  restored=Book(.00000001,100_000_000);restored.snapshot(book.export())
  self.assertEqual(restored.bids,book.bids);self.assertEqual(restored.bins,book.bins)
  self.assertEqual(book.scale,100_000_000)

 def test_private_chart_keeps_small_quote_volumes_and_only_six_hours(self):
  with tempfile.TemporaryDirectory() as root:
   now=30*3600000
   store=Store(root);store.bin_epsilon=1e-14
   for minute in (now-7*3600000,now-60000):
    store.save_minute(minute,[['b',11,.00012345]],60000,.0000001,.0000002)
    store.db.execute('INSERT INTO prices VALUES(?,?,?)',(minute,minute,.00000011))
   store.db.execute('INSERT INTO cvd VALUES(?,?,?,?)',(now-60000,0,1,1))
   store.meta('last_trade_ms',now);store.flush()
   conf={**config(root), 'private_pair':True,'price_scale':100_000_000,'price_precision':8,'price_step':.00000001,'base_price_step':.00000001,'window_hours':6,'trade_scale':100_000_000,'symbol':'TINYBTC','quote_asset':'BTC'}
   data=chart_data(conf,'1d',end_ms=now,aligned=True)
   self.assertEqual(data['requested_start_ms'],now-6*3600000)
   self.assertGreaterEqual(data['start_ms'],now-6*3600000)
   self.assertEqual(data['quote_asset'],'BTC')
   self.assertAlmostEqual(next(row[0] for row in data['cvd'] if row is not None),.00000001,places=12)
   self.assertTrue(any(v==.00012345 for col in data['liquidity'] if col for _,v in col))
   self.assertEqual(price_settings(conf,'1d',.00000003,5)[0],.00000003)
   with self.assertRaises(ValueError):price_settings(conf,'1d',.000000015,5)
   store.close()

 def test_fractional_day_retention_keeps_btc_isolated(self):
  with tempfile.TemporaryDirectory() as root:
   public=Path(root)/'archive.sqlite';public.write_bytes(b'public-btc-untouched')
   store=Store(Path(root)/'private-pairs'/'ETHBTC',auto_vacuum=True)
   now=30*3600000
   for minute in (now-7*3600000,now-5*3600000):
    store.save_minute(minute,[['b',1,.1]],60000,.1,.2)
   store.prune(.25,now)
   self.assertEqual(store.db.execute('SELECT count(*) FROM liquidity').fetchone()[0],1)
   self.assertEqual(public.read_bytes(),b'public-btc-untouched');store.close()


class PairManagerTests(unittest.TestCase):
 def make(self,root):
  manager=PairManager(config(root))
  def fetch(conf,path,**params):return CATALOG if path.endswith('exchangeInfo') else {'price':'.03'}
  with patch('mtanalog.private_pairs.rest',side_effect=fetch):manager.add('ETHBTC')
  return manager

 def test_validated_catalog_isolated_registry_settings_and_restart(self):
  with tempfile.TemporaryDirectory() as root:
   manager=self.make(root)
   conf=json.loads((manager.pair_path('ETHBTC')/'collector-config.json').read_text())
   self.assertEqual(conf['retention_days'],.25);self.assertEqual(conf['window_hours'],6)
   self.assertEqual(conf['base_price_step'],.000001);self.assertEqual(conf['price_scale'],1_000_000)
   self.assertIn('ethbtc@depth',conf['stream_url']);self.assertEqual(len(manager.catalog_data),1)
   settings=dict(step=.000003,range=4,filter=dict(type='manual',volume=.1,lower=.01,upper=.5))
   manager.save_settings('ETHBTC',settings)
   restored=PairManager(config(root))
   self.assertEqual(restored.list()['pairs'][0]['settings'],settings)
   for name in ('BTCUSDT','../BTCUSDT','ethbtc','ETHBTC/../../','',None):
    with self.assertRaises(ValueError):symbol_name(name)
   with self.assertRaises(ValueError):manager.save_settings('ETHBTC',{**settings,'step':.0000001})
   with self.assertRaises(ValueError):manager.save_settings('ETHBTC',{**settings,'range':float('nan')})

 def test_delete_stops_writer_before_removing_only_selected_archive(self):
  with tempfile.TemporaryDirectory() as root:
   manager=self.make(root);public=Path(root)/'archive.sqlite';public.write_bytes(b'btc')
   settings=manager.settings('ETHBTC',manager.state['pairs']['ETHBTC']);manager.save_settings('ETHBTC',settings)
   path=manager.pair_path('ETHBTC');(path/'test').write_text('pair data')
   with self.assertRaises(ValueError):manager.delete('BTCUSDT')
   manager.delete('ETHBTC')
   with self.assertRaises(ValueError):manager.chart('ETHBTC')
   def stop(name):self.assertEqual(name,'ETHBTC');self.assertTrue(path.exists())
   with patch.object(manager,'stop_pair',side_effect=stop):manager.reconcile()
   self.assertFalse(path.exists());self.assertEqual(public.read_bytes(),b'btc')
   self.assertEqual(manager.state['settings']['ETHBTC'],settings)
   self.assertEqual(manager.list()['pairs'],[])

 def test_delete_refuses_live_writer_even_if_pid_file_is_missing(self):
  with tempfile.TemporaryDirectory() as root:
   manager=self.make(root);path=manager.pair_path('ETHBTC')
   manager.delete('ETHBTC')
   with (path/'collector.lock').open('a') as held:
    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with self.assertRaises(BlockingIOError):manager.reconcile()
    self.assertTrue(path.exists());self.assertIn('ETHBTC',manager.state['pairs'])
   manager.reconcile();self.assertFalse(path.exists())

 def test_existing_worker_not_duplicated_and_symlink_rejected(self):
  with tempfile.TemporaryDirectory() as root:
   manager=self.make(root)
   with patch.object(manager,'pid',return_value=123),patch('mtanalog.private_pairs.subprocess.Popen') as spawn:
    manager.reconcile();spawn.assert_not_called()
   with tempfile.TemporaryDirectory() as elsewhere:
    (manager.directory/'SOLUSDT').symlink_to(elsewhere,target_is_directory=True)
    with self.assertRaises(ValueError):manager.pair_path('SOLUSDT')


class PrivateAccessTests(unittest.TestCase):
 def test_token_storage_rotation_and_secure_cookie_api_boundary(self):
  with tempfile.TemporaryDirectory() as root:
   conf=config(root);link=Path(root)/'link.txt';create_link(conf,'https://charts.example',link)
   token=link.read_text().strip().split('#key=')[1]
   self.assertEqual(link.stat().st_mode&0o777,0o600)
   self.assertNotIn(token,(Path(root)/'private-access.json').read_text())
   self.assertTrue(authorized(conf,COOKIE+'='+token));self.assertFalse(valid_token(conf,'x'*43))
   service=ChartService(conf);service.private=Mock();service.private.list.return_value={'pairs':[]}
   server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(service));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   base='http://127.0.0.1:'+str(server.server_port)
   def req(path,body=None,owner=False,origin=None):
    headers={}
    if owner:headers['Cookie']=COOKIE+'='+token
    if body is not None:headers.update({'Content-Type':'application/json','Origin':origin or base})
    return urlopen(Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers))
   try:
    for path in ('pairs','catalog','chart?symbol=ETHBTC'):
     with self.assertRaises(HTTPError) as caught:req('/api/private/'+path)
     self.assertEqual(caught.exception.code,403)
    for path in ('start','delete','settings'):
     with self.assertRaises(HTTPError) as caught:req('/api/private/'+path,{'symbol':'ETHBTC'})
     self.assertEqual(caught.exception.code,403)
    service.private.list.assert_not_called();service.private.delete.assert_not_called()
    with req('/api/private/pairs',owner=True) as r:self.assertEqual(json.load(r),{'pairs':[]})
    with req('/api/private/session',{'token':token}) as r:
     cookie=r.headers['Set-Cookie'];self.assertIn('Secure',cookie);self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
    with self.assertRaises(HTTPError) as caught:req('/api/private/delete',{'symbol':'ETHBTC'},True,'https://foreign.example')
    self.assertEqual(caught.exception.code,403);service.private.delete.assert_not_called()
    with req('/') as r:
     self.assertNotIn(b'data-private="true"',r.read());self.assertIsNone(r.headers.get('Set-Cookie'))
    with patch('mtanalog.web.chart_data',return_value={'empty':True}):
     with req('/api/chart?symbol=ETHBTC') as r:self.assertEqual(r.status,200)
    create_link(conf,'https://charts.example',link);self.assertFalse(valid_token(conf,token))
   finally:server.shutdown();server.server_close();thread.join()
