"""Bounded owner-selected Spot collectors. Public BTC data is never a target."""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from decimal import Decimal

from .collector import rest
from .private_access import private_write
from .webdata import price_settings

SYMBOL = re.compile(r'[A-Z0-9]{2,32}\Z')


def symbol_name(value):
    if not isinstance(value,str) or not SYMBOL.fullmatch(value):
        raise ValueError('Некорректная пара Binance Spot.')
    if value == 'BTCUSDT':
        raise ValueError('Публичный BTCUSDT защищён от изменения и удаления.')
    return value


class PairManager:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['data_dir']).resolve()
        self.directory = self.root/'private-pairs'
        self.directory.mkdir(exist_ok=True)
        if self.directory.is_symlink(): raise ValueError('Private data directory must not be a symlink')
        self.registry = self.root/'private-pairs.json'
        self.lock = threading.RLock()
        self.compute = threading.Lock()
        self.catalog_lock = threading.Lock()
        self.catalog_time = 0; self.catalog_data = {}
        self.services = {}; self.children = {}; self.retry = {}
        self.stopping = threading.Event(); self.thread = None
        self.process_lock = None
        self.max_active = int(config.get('private_max_active',4))
        try:
            self.state = json.loads(self.registry.read_text())
            assert isinstance(self.state['pairs'],dict) and isinstance(self.state['settings'],dict)
            for name in self.state['pairs']: symbol_name(name)
        except FileNotFoundError:
            self.state = {'pairs':{},'settings':{}}
        except (ValueError, KeyError, AssertionError):
            raise ValueError('Private pair registry is invalid; retaining files for recovery') from None

    def persist(self):
        private_write(self.registry,json.dumps(self.state,allow_nan=False)+'\n')

    def catalog(self):
        with self.catalog_lock:
            if time.monotonic()-self.catalog_time < 3600 and self.catalog_data:
                return list(self.catalog_data.values())
            response = rest({**self.config,'rest_timeout':8},'/api/v3/exchangeInfo',permissions='SPOT',showPermissionSets='false')
            data = {}
            for item in response['symbols']:
                if item.get('status') != 'TRADING' or item.get('isSpotTradingAllowed') is not True:
                    continue
                name=item['symbol']
                if not SYMBOL.fullmatch(name): continue
                tick=next((Decimal(f['tickSize']) for f in item['filters'] if f['filterType']=='PRICE_FILTER'),None)
                if tick is None or tick<=0:continue
                precision=max(0,-tick.normalize().as_tuple().exponent)
                if precision>12:continue
                data[name]={'symbol':name,'base_asset':item['baseAsset'],'quote_asset':item['quoteAsset'],
                            'tick_size':float(tick),'price_scale':10**precision,'price_precision':precision}
            self.catalog_data=data;self.catalog_time=time.monotonic()
            return list(data.values())

    def pair_path(self, name):
        name=symbol_name(name); path=self.directory/name
        if path.is_symlink() or path.resolve().parent!=self.directory:
            raise ValueError('Unsafe pair directory')
        return path

    def settings(self, name, item):
        return self.state['settings'].get(name,dict(step=item['price_step'],range=5,
                    filter=dict(type='usdt',volume=0,lower=0,upper=1_000_000)))

    def list(self):
        with self.lock:
            result=[]
            for name,item in self.state['pairs'].items():
                path=self.pair_path(name)
                used=sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink()) if path.exists() and not item.get('deleting') else 0
                status='deleting' if item.get('deleting') else 'starting'
                first=last=None
                if not item.get('deleting') and (path/'archive.sqlite').exists():
                    try:
                        db=sqlite3.connect(f'file:{path}/archive.sqlite?mode=ro',uri=True,timeout=1)
                        try:
                            row=db.execute("SELECT value FROM meta WHERE key='collector'").fetchone()
                            status=json.loads(row[0]).get('state','starting') if row else status
                            first,last=db.execute('SELECT min(minute),max(minute) FROM liquidity').fetchone()
                        finally:db.close()
                    except (sqlite3.Error,ValueError):status='starting'
                result.append({**{k:item[k] for k in ('symbol','base_asset','quote_asset','tick_size','price_precision','created_ms')},
                               'state':status,'bytes':used,'first_ms':first,'last_ms':last,
                               'settings':self.settings(name,item)})
            return {'pairs':result,'retention_hours':6,'max_active':self.max_active}

    def add(self, name):
        name=symbol_name(name)
        self.catalog()
        if name not in self.catalog_data:raise ValueError('Выберите доступную торговую пару Binance Spot.')
        with self.lock:
            if name in self.state['pairs']:
                if self.state['pairs'][name].get('deleting'):raise ValueError('Дождитесь удаления данных пары.')
                return self.list()
            if len(self.state['pairs'])>=self.max_active:
                raise ValueError(f'Одновременно доступно {self.max_active} личных пар. Остановите одну перед добавлением новой.')
            if shutil.disk_usage(self.root).free < self.config.get('min_free_gib',3)*2**30:
                raise ValueError('Недостаточно свободного места для нового сбора.')
            item=dict(self.catalog_data[name]); tick=Decimal(str(item['tick_size']))
            price=Decimal(str(rest({**self.config,'rest_timeout':8},'/api/v3/ticker/price',symbol=name)['price']))
            item.update(created_ms=int(time.time()*1000),price_step=float(max(1,int(price*Decimal('.001')/tick))*tick))
            path=self.pair_path(name);path.mkdir(exist_ok=True)
            conf={**self.config,**item,'private_pair':True,'symbol':name,'data_dir':str(path),
                  'base_price_step':item['tick_size'],'trade_scale':100_000_000,'retention_days':.25,'window_hours':6,
                  'prune_interval_seconds':60,'checkpoint_seconds':60,
                  'depth_snapshot_seconds':15,'min_liquidity_usdt':0,
                  'stream_url':'wss://data-stream.binance.vision/stream?streams='+name.lower()+'@depth/'+name.lower()+'@aggTrade'}
            private_write(path/'collector-config.json',json.dumps(conf)+'\n')
            self.state['pairs'][name]=item;self.persist()
            return self.list()

    def save_settings(self, name, value):
        name=symbol_name(name)
        with self.lock:
            item=self.state['pairs'].get(name)
            if not item or item.get('deleting'):raise ValueError('Сбор пары не запущен.')
            if not isinstance(value,dict):raise ValueError('Некорректные настройки.')
            step,margin=price_settings({**item,'private_pair':True,'base_price_step':item['tick_size']},'1d',value.get('step'),value.get('range'))
            filt=value.get('filter',{})
            if not isinstance(filt,dict):raise ValueError('Некорректный фильтр.')
            if filt.get('type') not in ('usdt','manual'):raise ValueError('Некорректный фильтр.')
            for key in ('volume','lower','upper'):
                n=filt.get(key)
                if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not 0<=n<=1e18:
                    raise ValueError('Некорректная граница объёма.')
            if filt['upper']<=filt['lower']:raise ValueError('Верхняя граница должна быть больше нижней.')
            self.state['settings'][name]=dict(step=step,range=margin,filter={k:filt[k] for k in ('type','volume','lower','upper')})
            self.persist()

    def delete(self, name):
        name=symbol_name(name)
        with self.lock:
            if name not in self.state['pairs']:raise ValueError('Пара не найдена.')
            self.state['pairs'][name]['deleting']=True;self.persist()
        return {'accepted':True}

    def chart(self, name, step=None, margin=None, since=None):
        name=symbol_name(name)
        from .web import ChartService
        with self.lock:
            item=self.state['pairs'].get(name)
            if not item or item.get('deleting'):raise ValueError('Сбор пары не запущен.')
            if name not in self.services:
                conf=json.loads((self.pair_path(name)/'collector-config.json').read_text())
                service=ChartService(conf);service.compute=self.compute
                self.services[name]=service
            service=self.services[name]
        return service.update('1d',step,margin,since)

    def pid(self,name):
        try:
            path=self.pair_path(name)/'collector-config.json'
            pid=int((path.parent/'pair.pid').read_text())
            proc=Path('/proc')/str(pid)
            args=(proc/'cmdline').read_bytes().split(b'\0')
            if proc.stat().st_uid==os.getuid() and b'mtanalog.pairs_worker' in args and str(path).encode() in args:
                return pid
        except (OSError,ValueError):pass
        return None

    def stop_pair(self,name):
        pid=self.pid(name)
        if pid:
            os.kill(pid,signal.SIGTERM)
            deadline=time.monotonic()+40
            while self.pid(name)==pid and time.monotonic()<deadline:time.sleep(.2)
            if self.pid(name)==pid:
                os.kill(pid,signal.SIGKILL)
                deadline=time.monotonic()+5
                while self.pid(name)==pid and time.monotonic()<deadline:time.sleep(.1)
                if self.pid(name)==pid:raise RuntimeError('Pair writer is still running; retaining archive')
        child=self.children.pop(name,None)
        if child:
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:raise RuntimeError('Pair writer did not stop')

    def reconcile(self):
        with self.lock: items=list(self.state['pairs'].items())
        for name,item in items:
            if self.stopping.is_set():return
            if item.get('deleting'):
                with self.compute:
                    self.stop_pair(name)
                    path=self.pair_path(name)
                    from contextlib import ExitStack
                    with ExitStack() as locks:
                        for filename in ('pair-worker.lock','collector.lock','market.lock'):
                            lockpath=path/filename
                            if lockpath.exists():
                                guard=locks.enter_context(lockpath.open('a'))
                                fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        if path.exists():shutil.rmtree(path)
                    with self.lock:
                        self.services.pop(name,None);self.state['pairs'].pop(name,None);self.persist()
            elif (not self.pid(name) and time.monotonic()>=self.retry.get(name,0)
                  and shutil.disk_usage(self.root).free >= self.config.get('min_free_gib',3)*2**30):
                previous=self.children.pop(name,None)
                if previous:previous.poll()
                path=self.pair_path(name)/'collector-config.json'
                child=subprocess.Popen([sys.executable,'-m','mtanalog.pairs_worker',str(path)],
                      cwd=Path(__file__).resolve().parent.parent,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                self.children[name]=child;self.retry[name]=time.monotonic()+60

    def start(self):
        self.process_lock=(self.root/'private-manager.lock').open('a')
        try:fcntl.flock(self.process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except Exception:
            self.process_lock.close();raise
        def run():
            import logging
            try:
                while not self.stopping.is_set():
                    try:self.reconcile()
                    except Exception:logging.getLogger(__name__).exception('Private pair reconciliation failed')
                    self.stopping.wait(3)
            finally:
                for name in list(self.state['pairs']):self.stop_pair(name)
                self.process_lock.close()
        self.thread=threading.Thread(target=run,name='private-pairs',daemon=True);self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:self.thread.join(timeout=50)
