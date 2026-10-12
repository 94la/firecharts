import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from urllib.parse import urlsplit, parse_qs
from http.server import ThreadingHTTPServer

from mtanalog.access import COOKIE, invite, permission
from mtanalog.web import ChartService, handler_for


def token(path):
    return parse_qs(urlsplit(Path(path).read_text().strip()).fragment)['access'][0]


class AccessTests(unittest.TestCase):
    def test_deadline_rotation_restart_owner_and_private_storage(self):
        with tempfile.TemporaryDirectory() as root:
            config={'data_dir':root,'web_access_restricted':True}
            guest=Path(root)/'guest.txt';owner=Path(root)/'owner.txt'
            end=invite(config,'https://chart.example',guest,owner,now=100)
            first=token(guest);permanent=token(owner)
            self.assertEqual(end,100+12*3600)
            self.assertEqual(permission(config,first,now=end-.01),(True,end))
            self.assertEqual(permission(config,first,now=end),(False,None))
            self.assertEqual(permission(config,permanent,now=end+86400),(True,None))
            saved=(Path(root)/'web-access.json').read_text()
            self.assertNotIn(first,saved);self.assertNotIn(permanent,saved)
            for path in [guest,owner,Path(root)/'web-access.json']:
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            invite(config,'https://chart.example',guest,now=200)
            self.assertEqual(permission(config,first,now=201),(False,None))
            self.assertTrue(permission(config,token(guest),now=201)[0])
            self.assertTrue(permission(config,permanent,now=201)[0])

    def test_permanent_guest_rotation_and_expiring_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            config={'data_dir':root,'web_access_restricted':True}
            guest=Path(root)/'guest';owner=Path(root)/'owner'
            self.assertIsNone(invite(config,'https://chart.example',guest,owner,hours=None,now=100))
            permanent=token(guest)
            self.assertEqual(permission(config,permanent,now=10**12),(True,None))
            invite(config,'https://chart.example',guest,hours=1,now=200)
            self.assertFalse(permission(config,permanent,now=201)[0])
            self.assertEqual(permission(config,token(guest),now=3800),(False,None))
            self.assertEqual(permission(config,token(owner),now=10**12),(True,None))

    def test_fail_closed_missing_malformed_state_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            config={'data_dir':root,'web_access_restricted':True}
            self.assertFalse(permission(config,'x'*43)[0])
            (Path(root)/'web-access.json').write_text('[]')
            self.assertFalse(permission(config,'x'*43)[0])
            for base in ['http://chart.example','https://chart.example/?key=x','https://user:pass@chart.example']:
                with self.assertRaises(ValueError):invite(config,base,Path(root)/'guest')
            with self.assertRaises(ValueError):invite(config,'https://chart.example',Path(root)/'guest')
            self.assertEqual(permission({'data_dir':root},''),(True,None))

    def test_public_http_has_no_gate_or_monitoring(self):
        with tempfile.TemporaryDirectory() as root:
            service=ChartService({'data_dir':root,'price_step':200})
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(service))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(base+'/') as response:
                    page=response.read()
                    self.assertIn(b'id="dashboard"',page)
                    self.assertNotIn(b'id="server-load"',page)
                    self.assertIsNone(response.headers.get('Set-Cookie'))
                with patch('mtanalog.web.chart_data',return_value={'empty':True}):
                    with urllib.request.urlopen(base+'/api/chart') as response:
                        self.assertEqual(response.status,200)
                        self.assertIsNone(response.headers.get('X-Access-Expires'))
                for route in ['/api/load','/access.js','/data/web-access.json']:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base+route)
                    self.assertEqual(error.exception.code,404)
                with urllib.request.urlopen(base+'/enter') as response:
                    self.assertEqual(response.url,base+'/')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base+'/api/access',data=b'{}'))
                self.assertEqual(error.exception.code,404)
            finally:
                server.shutdown();server.server_close();thread.join()
