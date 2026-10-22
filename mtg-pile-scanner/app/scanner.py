"""Pile scanning: tiled OCR over the whole image, then fuzzy-match every text line to card names."""
import re, threading, time
import cv2, numpy as np
from . import art, cardfinder, scryfall

TILE, OVERLAP = 1280, 400
TYPE_LINE = re.compile(r"[\u2014\u2013]|\s-\s|^(legendary |basic |snow |tribal |artifact )*"
                       r"(creature|artifact|enchantment|land|planeswalker|instant|sorcery|battle)\b.*-", re.I)
BASICS = {"plains", "island", "swamp", "mountain", "forest", "wastes"}
_engine, _lock = None, threading.Lock()


def _load_engine():
    try:
        from rapidocr import RapidOCR  # current package (Python 3.13+ friendly)
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR  # legacy package
    return RapidOCR()


def _ocr(img):
    """Returns list of (box_points, text, score) for both old and new RapidOCR APIs."""
    global _engine
    with _lock:
        if _engine is None:
            _engine = _load_engine()
        out = _engine(img)
    if hasattr(out, "boxes"):
        if out.boxes is None or out.txts is None:
            return []
        return list(zip(np.asarray(out.boxes).tolist(), out.txts, out.scores))
    res = out[0] if isinstance(out, tuple) else out
    return res or []


def decode(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def _starts(n):
    if n <= TILE:
        return [0]
    step, xs = TILE - OVERLAP, [0]
    while xs[-1] + TILE < n:
        xs.append(min(xs[-1] + step, n - TILE))
    return xs


def _unrot(x, y, code, W, H):
    if code == cv2.ROTATE_90_CLOCKWISE:
        return y, H - 1 - x
    if code == cv2.ROTATE_90_COUNTERCLOCKWISE:
        return W - 1 - y, x
    return W - 1 - x, H - 1 - y


def _set_hint(text):
    """Parse collector lines like '051/281 • SNC • EN' -> (set_code, number)."""
    if len(text) > 40 or not re.search(r"[\d•·*]", text):
        return None
    code = next((t.lower() for t in re.findall(r"\b[A-Z][A-Z0-9]{2,4}\b", text)
                 if t.lower() in scryfall.INDEX["sets"]), None)
    if not code:
        return None
    m = re.search(r"\b(\d{1,4})\s*/\s*\d{1,4}\b", text) or re.match(r"\s*(\d{1,4})\b", text)
    return code, (m.group(1) if m else None)


def scan(img, thorough=False, use_art=True, fast=False):
    t0 = time.time()
    H0, W0 = img.shape[:2]
    longest = max(H0, W0)
    target = 1600 if fast else 2200  # live scans trade a little accuracy for speed
    cap = 1800 if fast else (3600 if thorough else 2600)  # bigger photos are downscaled: same accuracy, far less OCR time
    scale = min(2.0, target / longest) if longest < target else (cap / longest if longest > cap else 1.0)
    work = img if abs(scale - 1) < 0.01 else cv2.resize(
        img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    Hw, Ww = work.shape[:2]
    passes = [None] + ([cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180] if thorough else [])

    lines = []
    for code in passes:
        im = work if code is None else cv2.rotate(work, code)
        h, w = im.shape[:2]
        for y in _starts(h):
            for x in _starts(w):
                tile = im[y:y + TILE, x:x + TILE]
                if cv2.meanStdDev(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY))[1][0][0] < 8:
                    continue  # blank table/background: nothing to read
                for box, text, conf in _ocr(tile):
                    pts = [(px + x, py + y) for px, py in box]
                    if code is not None:
                        pts = [_unrot(px, py, code, Ww, Hw) for px, py in pts]
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    lines.append({"text": text, "conf": float(conf), "rot": code is not None,
                                  "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})

    dets = []
    for L in lines:
        compact = L["text"].replace(" ", "")
        letters = sum(ch.isalpha() for ch in compact)
        if L["conf"] < 0.5 or letters < 3 or letters < 0.6 * len(compact):
            continue  # numbers / symbols / OCR noise can't be a card title
        if TYPE_LINE.search(L["text"]):
            continue  # a type line ("Creature - Goblin"), not a title
        m = scryfall.match_text(L["text"])
        if m:
            dets.append({**L, "key": m[0], "score": m[1]})
    dets.sort(key=lambda d: (-d["score"], -d["conf"]))
    kept = []
    for d in dets:  # drop duplicate reads of the same title from overlapping tiles
        w, h = d["x1"] - d["x0"], d["y1"] - d["y0"]
        cx, cy = (d["x0"] + d["x1"]) / 2, (d["y0"] + d["y1"]) / 2
        dup = False
        for k in kept:
            if k["key"] != d["key"]:
                continue
            kw, kh = k["x1"] - k["x0"], k["y1"] - k["y0"]
            kx, ky = (k["x0"] + k["x1"]) / 2, (k["y0"] + k["y1"]) / 2
            if abs(cx - kx) < 0.25 * min(w, kw) and abs(cy - ky) < 0.5 * min(h, kh):
                dup = True
                break
        if not dup:
            kept.append(d)

    def on_basic_land_line(d):  # "Basic Land - Mountain" split into pieces must not count as a Mountain card
        hd = max(d["y1"] - d["y0"], 1)
        cy = (d["y0"] + d["y1"]) / 2
        return any(L["text"] != d["text"] and re.search(r"basic|land", L["text"], re.I)
                   and abs((L["y0"] + L["y1"]) / 2 - cy) < 0.8 * hd for L in lines)

    kept = [d for d in kept if not (d["key"] in BASICS and on_basic_land_line(d))]

    hints = []
    for L in lines:
        if not L["rot"]:
            hh = _set_hint(L["text"])
            if hh:
                hints.append({**L, "code": hh[0], "num": hh[1]})

    art_hits, art_quads = [], 0
    if use_art and art.size() > 0:
        centers = [((d["x0"] + d["x1"]) / 2 / scale, (d["y0"] + d["y1"]) / 2 / scale) for d in kept]
        for q in cardfinder.find_rects(img):
            art_quads += 1
            if cardfinder.explained(q, centers):
                continue  # a title already identified this card
            cands = []
            for im, fn in ((cardfinder.warp_card(img, q), art.art_descriptor),
                           (cardfinder.warp_art(img, q), art.art_descriptor_from_art)):
                for r in (im, cv2.rotate(im, cv2.ROTATE_180)):
                    m = art.match(fn(r))
                    if m:
                        cands.append(m)
            best = max(cands, key=lambda m: m[1], default=None)
            if best and best[1] >= art.ART_MIN_SIM and best[2] >= art.ART_MIN_MARGIN:
                art_hits.append((q, best))
        art_hits.sort(key=lambda h: -h[1][1])
        uniq = []
        for q, m in art_hits:  # same card found as both "whole card" and "art window" -> keep the best
            c, side = q.mean(axis=0), np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[1])
            if any(m[0] == m2[0] and np.linalg.norm(c - q2.mean(axis=0)) < 0.35 * side for q2, m2 in uniq):
                continue
            uniq.append((q, m))
        art_hits = uniq

    out = []
    for d in kept:
        bh, best = max(d["y1"] - d["y0"], 1), None
        if not d["rot"]:
            for hl in hints:
                dy = hl["y0"] - d["y1"]
                if 8 * bh <= dy <= 32 * bh and abs(hl["x0"] - d["x0"]) <= 4 * bh and (best is None or dy < best[0]):
                    best = (dy, hl)
        sh = (best[1]["code"], best[1]["num"]) if best else (None, None)
        card_id, verified = scryfall.pick_printing(d["key"], *sh)
        out.append((d, card_id, verified))
    info = scryfall.card_info({c for _, c, _ in out})
    dets_out = []
    for d, card_id, verified in out:
        c = info.get(card_id)
        if not c:
            continue
        dets_out.append({
            "card_id": card_id, "name": c["name"], "set_code": c["set_code"], "set_name": c["set_name"],
            "collector_number": c["collector_number"], "image_url": c["image_url"],
            "score": round(d["score"], 1), "verified": verified, "ocr_text": d["text"],
            "box": [round(d["x0"] / scale), round(d["y0"] / scale),
                    round((d["x1"] - d["x0"]) / scale), round((d["y1"] - d["y0"]) / scale)]})
    if art_hits:
        info2 = scryfall.card_info({b[0] for _, b in art_hits})
        for q, (cid, sim, margin) in art_hits:
            c = info2.get(cid)
            if not c:
                continue
            x0, y0 = q.min(axis=0)
            x1, y1 = q.max(axis=0)
            dets_out.append({
                "card_id": cid, "name": c["name"], "set_code": c["set_code"], "set_name": c["set_name"],
                "collector_number": c["collector_number"], "image_url": c["image_url"],
                "score": round(sim * 100, 1), "verified": False, "ocr_text": "", "method": "art",
                "box": [round(float(x0)), round(float(y0)), round(float(x1 - x0)), round(float(y1 - y0))]})
    return {"width": W0, "height": H0, "detections": dets_out, "elapsed": round(time.time() - t0, 1),
            "art_found": len(art_hits), "quads": art_quads, "art_index": art.size(),
            "art_total": art.STATE["total"]}
