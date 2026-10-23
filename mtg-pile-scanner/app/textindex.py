"""Identify partly hidden cards from fragments of their rules text (word-bigram index with IDF weights)."""
import re, zlib
import numpy as np
from rapidfuzz import fuzz
from . import db

_STRIP = re.compile(r"\{[^}]*\}")
_WORD = re.compile(r"[a-z]{2,}")
IDX = {"u": None, "start": None, "cnt": None, "idf": None, "cards": None, "names": [], "texts": []}
MAX_POSTINGS = 300   # ignore bigrams shared by too many cards
MIN_SCORE = 14.0     # about two distinctive word pairs
MIN_WORDS = 5


def words(s):
    return _WORD.findall(_STRIP.sub(" ", (s or "").lower()))


def _grams(ws):
    return {zlib.crc32(f"{a} {b}".encode()) for a, b in zip(ws, ws[1:])}


def build():
    with db.LOCK:
        rows = db.get().execute("SELECT name, text FROM oracle WHERE text != ''").fetchall()
    if not rows:
        IDX["u"] = None
        return
    h, c, texts = [], [], []
    for i, r in enumerate(rows):
        ws = words(r["text"])
        texts.append(" ".join(ws))
        g = _grams(ws)
        h.extend(g)
        c.extend([i] * len(g))
    h, c = np.array(h, np.uint32), np.array(c, np.int32)
    o = np.argsort(h, kind="stable")
    h, c = h[o], c[o]
    u, start, cnt = np.unique(h, return_index=True, return_counts=True)
    IDX.update(u=u, start=start, cnt=cnt, idf=np.log(len(rows) / cnt).astype(np.float32), cards=c,
               names=[r["name"] for r in rows], texts=texts)


def ready():
    return IDX["u"] is not None


def match_line(text):
    """Return {name, oid, tscore} if this OCR line is clearly a fragment of one card's rules text."""
    ws = words(text)
    if len(ws) < MIN_WORDS or not ready():
        return None
    u = IDX["u"]
    g = np.array(sorted(_grams(ws)), np.uint32)
    pos = np.minimum(np.searchsorted(u, g), len(u) - 1)
    scores = {}
    for j in pos[u[pos] == g]:
        n = int(IDX["cnt"][j])
        if n > MAX_POSTINGS:
            continue
        s, w = int(IDX["start"][j]), float(IDX["idf"][j])
        for ci in IDX["cards"][s:s + n]:
            scores[int(ci)] = scores.get(int(ci), 0.0) + w
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda t: -t[1])
    best, s1 = ranked[0]
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0
    if s1 < MIN_SCORE or s1 < 1.4 * s2:
        return None
    if fuzz.partial_ratio(" ".join(ws), IDX["texts"][best]) < 80:
        return None
    return {"name": IDX["names"][best], "oid": best, "tscore": s1}
