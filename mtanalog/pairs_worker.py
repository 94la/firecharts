"""One independent six-hour archive writer for one owner-selected Spot pair."""
import asyncio
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading

from .collector import collect
from .market import start_worker


def main():
    path=Path(sys.argv[1]).resolve()
    config=json.loads(path.read_text())
    root=Path(config['data_dir']).resolve()
    if path.parent!=root or root.name!=config['symbol'] or not config.get('private_pair') or config['retention_days']!=.25:
        raise ValueError('Invalid private collector configuration')
    # A duplicate launch must never overwrite the PID of the existing writer.
    with (root/'pair-worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        handler=RotatingFileHandler(root/'collector.log',maxBytes=1_000_000,backupCount=1)
        logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
        pidfile=root/'pair.pid';pidfile.write_text(str(os.getpid()))
        stop=threading.Event()
        market=start_worker(root,stop,symbol=config['symbol'],retention_hours=6,
                            earliest=config['created_ms']//60000*60000,rest_url=config['rest_url'])
        try:
            asyncio.run(collect(config))
        finally:
            stop.set();market.join(timeout=12)
            if pidfile.exists() and pidfile.read_text()==str(os.getpid()):pidfile.unlink()


if __name__=='__main__':main()
