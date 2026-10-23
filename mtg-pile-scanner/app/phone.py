"""iPhone-as-camera link: the phone opens a small web page over Wi-Fi and sends photos / live frames here."""
import hmac, secrets, socket, threading, time
from collections import OrderedDict
from . import db

STATE = {"https_error": None, "frame": None, "frame_seq": 0, "last_seen": 0.0, "http_port": 8765, "https_port": None}
PHOTOS = OrderedDict()
_seq = 0
_lock = threading.Lock()


def token():
    t = db.meta_get("phone_token")
    if not t:
        t = secrets.token_urlsafe(8)
        db.meta_set("phone_token", t)
    return t


def check(t):
    return bool(t) and hmac.compare_digest(str(t), token())


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outgoing interface
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def touch():
    STATE["last_seen"] = time.time()


FRAME_COND = threading.Condition()   # the two servers run on different event loops, so use a thread condition


def set_frame(data):
    with FRAME_COND:
        STATE["frame"] = data
        STATE["frame_seq"] += 1
        FRAME_COND.notify_all()
    touch()


def wait_frame(after, timeout=8.0):
    """Block until a frame newer than `after` exists (or timeout); returns (seq, bytes|None)."""
    with FRAME_COND:
        FRAME_COND.wait_for(lambda: STATE["frame_seq"] != after, timeout)
        return STATE["frame_seq"], (STATE["frame"] if STATE["frame_seq"] != after else None)


def add_photo(data):
    global _seq
    with _lock:
        _seq += 1
        PHOTOS[_seq] = data
        while len(PHOTOS) > 20:
            PHOTOS.popitem(last=False)
    touch()
    return _seq


def pop_photo(pid):
    with _lock:
        return PHOTOS.pop(pid, None)


def poll():
    return {"connected": time.time() - STATE["last_seen"] < 6, "frame_seq": STATE["frame_seq"],
            "photos": list(PHOTOS.keys())}


def urls():
    ip, t = lan_ip(), token()
    http = f"http://{ip}:{STATE['http_port']}/phone?t={t}"
    https = f"https://{ip}:{STATE['https_port']}/phone?t={t}" if STATE["https_port"] else None
    return http, https


def selfcheck_https(port, cert_path, tries=10):
    """Make a real HTTPS request to our own secure port. Returns None if it works, else the error text."""
    import http.client, ssl as _ssl
    ctx = _ssl.create_default_context(cafile=str(cert_path))
    err = "no response"
    for _ in range(tries):
        time.sleep(0.5)
        try:
            c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
            c.request("GET", "/api/phone/poll")
            r = c.getresponse()
            r.read()
            c.close()
            if r.status == 200:
                return None
            err = f"unexpected HTTP status {r.status}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    return err


def svg_from_rows(rows):
    """Scalable SVG (explicit viewBox, so it can never be cropped) from rows of 0/1 modules that already include the quiet zone."""
    rows = [list(r) for r in rows]
    n = len(rows)
    cells = []
    for y, r in enumerate(rows):
        x = 0
        while x < n:  # merge horizontal runs of dark modules into one rectangle
            if r[x]:
                x0 = x
                while x < n and r[x]:
                    x += 1
                cells.append(f"M{x0} {y}h{x - x0}v1h-{x - x0}z")
            else:
                x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{"".join(cells)}" fill="#000"/></svg>')


def qr_svg(url):
    try:
        import segno
        return svg_from_rows(segno.make(url, error="m").matrix_iter(scale=1, border=4))
    except Exception:
        return ""
