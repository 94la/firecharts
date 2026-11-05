"""Owner-only capability; the public BTC dashboard never requires a session."""
import hashlib
from http.cookies import SimpleCookie, CookieError
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit

COOKIE = '__Host-firecharts-private'
REGISTRY = 'private-access.json'


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(value.encode())
            stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def valid_token(config, token):
    if not isinstance(token, str) or len(token) != 43:
        return False
    try:
        state = json.loads((Path(config['data_dir'])/REGISTRY).read_text())
        expected = state.get('hash', '')
        return isinstance(expected, str) and len(expected) == 64 and secrets.compare_digest(expected, hashlib.sha256(token.encode()).hexdigest())
    except (OSError, ValueError, AttributeError):
        return False


def authorized(config, header):
    try:
        cookies = SimpleCookie(); cookies.load(header or '')
        return COOKIE in cookies and valid_token(config, cookies[COOKIE].value)
    except (CookieError, ValueError):
        return False


def create_link(config, origin, output):
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTPS origin without a path')
    token = secrets.token_urlsafe(32)
    private_write(Path(config['data_dir'])/REGISTRY, json.dumps({'hash':hashlib.sha256(token.encode()).hexdigest()})+'\n')
    private_write(output, origin.rstrip('/')+'/private#key='+token+'\n')
