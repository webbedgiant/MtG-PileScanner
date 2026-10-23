"""Outbound HTTPS using the operating system's certificate store (fixes antivirus/proxy HTTPS inspection),
applied only to the downloads. The local web server keeps Python's standard TLS."""
import ssl
import requests
from requests.adapters import HTTPAdapter


class _OSTrustAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        try:
            import truststore
            kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            pass  # truststore missing: fall back to the default certificate bundle
        super().init_poolmanager(*args, **kwargs)


def new_session(pool=2):
    s = requests.Session()
    ad = _OSTrustAdapter(pool_connections=1, pool_maxsize=pool, max_retries=0)
    s.mount("https://", ad)
    return s


_shared = None


def get(*args, **kwargs):
    global _shared
    if _shared is None:
        _shared = new_session(pool=4)
    return _shared.get(*args, **kwargs)
