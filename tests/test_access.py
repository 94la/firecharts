import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from mtanalog.web import ChartService, handler_for


class PublicAccessTests(unittest.TestCase):
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
                    self.assertNotIn(b'MTF',page)
                    self.assertNotIn(b'data-indicator',page)
                    self.assertIsNone(response.headers.get('Set-Cookie'))
                with patch('mtanalog.web.chart_data',return_value={'empty':True}):
                    with urllib.request.urlopen(base+'/api/chart') as response:
                        self.assertEqual(response.status,200)
                        self.assertIsNone(response.headers.get('X-Access-Expires'))
                for route in ['/api/load','/access.js','/data/web-access.json','/api/mtf','/mtf.js','/owner','/api/capabilities']:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base+route)
                    self.assertEqual(error.exception.code,404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base+'/api/owner/session',data=b'{}'))
                self.assertEqual(error.exception.code,404)
                with urllib.request.urlopen(base+'/enter') as response:
                    self.assertEqual(response.url,base+'/')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(base+'/api/access',data=b'{}'))
                self.assertEqual(error.exception.code,404)
            finally:
                server.shutdown();server.server_close();thread.join()
