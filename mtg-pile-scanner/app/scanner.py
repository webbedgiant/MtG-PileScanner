"""Pile scanning: tiled OCR over the whole image, then fuzzy-match every text line to card names."""
import re, threading, time
import cv2, numpy as np
from . import art, cardfinder, scryfall, textindex

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


def scan(img, thorough=False, use_art=True, fast=False, max_side=None):
    t0 = time.time()
    H0, W0 = img.shape[:2]
    longest = max(H0, W0)
    target = 1600 if fast else 2200  # live scans trade a little accuracy for speed
    cap = 1800 if fast else (3600 if thorough else 2600)  # bigger photos are downscaled: same accuracy, far less OCR time
    if max_side:
        target = cap = max_side
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
            ix = min(d["x1"], k["x1"]) - max(d["x0"], k["x0"])
            iy = min(d["y1"], k["y1"]) - max(d["y0"], k["y0"])
            if (abs(cx - kx) < 0.25 * min(w, kw) and abs(cy - ky) < 0.5 * min(h, kh)) \
                    or (ix > 0.3 * min(w, kw) and iy > 0.3 * min(h, kh)):  # same text read twice (tile overlap)
                dup = True
                break
        if not dup:
            kept.append(d)

    # Short names ("Arena", "Mountain") also occur inside rules text, which is printed smaller than titles.
    long_h = [d["y1"] - d["y0"] for d in kept if len(d["key"]) >= 10 and d["score"] >= 95 and not d["rot"]]
    if len(long_h) >= 3:
        med = float(np.median(long_h))
        kept = [d for d in kept if not (len(d["key"]) <= 9 and not d["rot"] and (d["y1"] - d["y0"]) < 0.72 * med)]

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

    # --- rules-text pass: identifies cards whose title is hidden but whose body text is partly visible
    text_hits = []
    if textindex.ready():
        title_lines = {(d["x0"], d["y0"], d["text"]) for d in kept}
        cand = []
        for L in lines:
            if L["conf"] < 0.6 or L["rot"] or (L["x0"], L["y0"], L["text"]) in title_lines:
                continue
            r = textindex.match_line(L["text"])
            if r:
                cand.append({**L, **r})
        cand.sort(key=lambda t: -t["tscore"])
        for L in cand:
            hh = max(L["y1"] - L["y0"], 1)
            faces = {scryfall.norm(x) for x in L["name"].split(" // ")}
            near = lambda o: abs(o["x0"] - L["x0"]) < 20 * hh and abs(o["y0"] - L["y0"]) < 26 * hh
            if any(d["key"] in faces and near(d) for d in kept):
                continue  # this card was already read by its title
            if any(t["oid"] == L["oid"] and near(t) for t in text_hits):
                continue  # another line of a card we already have
            text_hits.append(L)

    art_hits, art_quads, sugg = [], 0, []
    if use_art and art.size() > 0:
        centers = [((d["x0"] + d["x1"]) / 2 / scale, (d["y0"] + d["y1"]) / 2 / scale) for d in kept + text_hits]
        for q in cardfinder.find_rects(img):
            art_quads += 1
            if cardfinder.explained(q, centers):
                continue  # a title or rules text already identified this card
            best = None
            for im, fn in ((cardfinder.warp_card(img, q), art.art_descriptor),
                           (cardfinder.warp_art(img, q), art.art_descriptor_from_art)):
                for r in (im, cv2.rotate(im, cv2.ROTATE_180)):
                    top = art.match_top(fn(r))
                    if top and (best is None or top[0][1] > best[0][1]):
                        best = top
            if not best:
                continue
            margin = best[0][1] - (best[1][1] if len(best) > 1 else 0.0)
            if best[0][1] >= art.ART_MIN_SIM and margin >= art.ART_MIN_MARGIN:
                art_hits.append((q, (best[0][0], best[0][1], margin)))
            elif best[0][1] >= art.ART_SUGGEST_MIN:
                sugg.append((q, best))
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
            "key": d["key"], "oracle": c.get("oracle_id"),
            "box": [round(d["x0"] / scale), round(d["y0"] / scale),
                    round((d["x1"] - d["x0"]) / scale), round((d["y1"] - d["y0"]) / scale)]})
    def bbox(q):
        x0, y0 = q.min(axis=0)
        x1, y1 = q.max(axis=0)
        return [round(float(x0)), round(float(y0)), round(float(x1 - x0)), round(float(y1 - y0))]

    sugg.sort(key=lambda t: -t[1][0][1])
    sugg = sugg[:10]
    extra_ids = {m[0] for _, m in art_hits} | {cid for _, top in sugg for cid, _ in top}
    tkeys = []
    for L in text_hits:
        by = scryfall.INDEX["by_name"].get(scryfall.norm(L["name"].split(" // ")[0]))
        if by:
            tkeys.append((L, by[0][0]))
            extra_ids.add(by[0][0])
    info2 = scryfall.card_info(extra_ids)

    def det(c, score, method, box, verified, text=""):
        return {"card_id": c["id"], "name": c["name"], "set_code": c["set_code"], "set_name": c["set_name"],
                "collector_number": c["collector_number"], "image_url": c["image_url"], "score": score,
                "verified": verified, "ocr_text": text, "method": method, "box": box}

    for L, cid in tkeys:
        c = info2.get(cid)
        if c:
            dets_out.append(det(c, 92.0, "text", [round(L["x0"] / scale), round(L["y0"] / scale),
                                                  round((L["x1"] - L["x0"]) / scale),
                                                  round((L["y1"] - L["y0"]) / scale)], False, L["text"]))
    for q, (cid, sim, margin) in art_hits:
        c = info2.get(cid)
        if c:
            dets_out.append(det(c, round(sim * 100, 1), "art", bbox(q), False))
    suggestions = [{"box": bbox(q), "cands": [
        {"card_id": cid, "name": info2[cid]["name"], "set_code": info2[cid]["set_code"],
         "collector_number": info2[cid]["collector_number"], "image_url": info2[cid]["image_url"],
         "sim": round(sim, 2)} for cid, sim in top if cid in info2]} for q, top in sugg]
    dets_out = merge_faces(dets_out)
    return {"width": W0, "height": H0, "detections": dets_out, "elapsed": round(time.time() - t0, 1),
            "art_found": len(art_hits), "text_found": len(tkeys), "suggestions": suggestions,
            "quads": art_quads, "rects": art_quads if (use_art and art.size() > 0) else None,
            "art_index": art.size(), "art_total": art.STATE["total"]}


def merge_faces(dets):
    """Split / adventure / double-faced cards print a second name on the same card ("My Precious // Allure of Power"),
    so one physical card can be read twice. The number of copies is the largest count of any single face name, not the sum."""
    groups = {}
    for d in dets:
        if d.get("key") and d.get("oracle"):
            groups.setdefault(d["oracle"], {}).setdefault(d["key"], []).append(d)
    drop = set()
    for by in groups.values():
        if len(by) < 2:
            continue
        best = max(by.values(), key=lambda L: (len(L), sum(x["score"] for x in L)))
        for L in by.values():
            if L is not best:
                drop.update(id(x) for x in L)
    return [d for d in dets if id(d) not in drop]


# ---------------------------------------------------------------- instant mode
TITLE_BOX = (0.035, 0.80, 0.030, 0.118)   # x0, x1, y0, y1 of the title bar on a rectified card (mana cost excluded)
INFO_BOX = (0.03, 0.50, 0.925, 0.985)     # collector number / set code line
TW = 720


def _strip(card, box):
    H, W = card.shape[:2]
    x0, x1, y0, y1 = box
    crop = card[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]
    return cv2.resize(crop, (TW, max(24, int(crop.shape[0] * TW / crop.shape[1]))), interpolation=cv2.INTER_CUBIC)


def _ocr_strips(strips):
    """OCR many small strips in ONE engine call by stacking them (with black gaps) into a single image."""
    gap = 24
    canvas = np.zeros((sum(s.shape[0] for s in strips) + gap * (len(strips) + 1), TW, 3), np.uint8)
    spans, y = [], gap
    for s in strips:
        canvas[y:y + s.shape[0]] = s
        spans.append((y, y + s.shape[0]))
        y += s.shape[0] + gap
    res = [[] for _ in strips]
    for box, text, conf in _ocr(canvas):
        cy = sum(p[1] for p in box) / len(box)
        for i, (a, b) in enumerate(spans):
            if a - gap / 2 <= cy < b + gap / 2:
                res[i].append({"text": text, "conf": float(conf), "x0": min(p[0] for p in box)})
                break
    return res


def _match_title(lines):
    ls = sorted((l for l in lines if l["conf"] >= 0.45), key=lambda l: l["x0"])
    cands = [l["text"] for l in ls] + ([" ".join(l["text"] for l in ls)] if len(ls) > 1 else [])
    best = None
    for t in cands:
        compact = t.replace(" ", "")
        if sum(ch.isalpha() for ch in compact) < 3 or TYPE_LINE.search(t):
            continue
        m = scryfall.match_text(t)
        if m and (best is None or m[1] > best[1]):
            best = m
    return best


def scan_instant(img, use_art=True):
    """Fast path for cards lying separately: find each card outline, read ONLY its title strip (one OCR call for all
    cards), confirm / fall back with the artwork fingerprint. Typically a fraction of a second."""
    t0 = time.time()
    H0, W0 = img.shape[:2]
    rects = cardfinder.find_whole_cards(img)[:10]
    if not rects:  # nothing card-shaped: quick single-tile read of the whole frame (dense piles, fans)
        r = scan(img, use_art=False, fast=True, max_side=1280)
        r.update(mode="instant-fallback", rects=0)
        return r
    cards = [cardfinder.warp_card(img, q, 2) for q in rects]
    res = [None] * len(rects)

    def run(cs, idxs):
        strips = []
        for c in cs:
            strips += [_strip(c, TITLE_BOX), _strip(c, INFO_BOX)]
        out = _ocr_strips(strips)
        for n, i in enumerate(idxs):
            m = _match_title(out[2 * n])
            if m and (res[i] is None or m[1] > res[i][1]):
                info = sorted(out[2 * n + 1], key=lambda l: l["x0"])
                texts = [l["text"] for l in info] + ([" ".join(l["text"] for l in info)] if len(info) > 1 else [])
                res[i] = (m[0], m[1], next((h for h in map(_set_hint, texts) if h), None))

    run(cards, list(range(len(cards))))
    todo = [i for i, r in enumerate(res) if r is None or r[1] < 90]
    if todo:  # card may be upside down: read its title at the other end
        run([cv2.rotate(cards[i], cv2.ROTATE_180) for i in todo], todo)

    tops = [None] * len(rects)
    if use_art and art.size() > 0:
        for i, c in enumerate(cards):
            if res[i] is not None and res[i][1] >= 100:
                continue  # exact title read: the art cannot add anything, skip the work
            for im in ((c,) if res[i] is not None else (c, cv2.rotate(c, cv2.ROTATE_180))):
                t = art.match_top(art.art_descriptor(im))
                if t and (tops[i] is None or t[0][1] > tops[i][0][1]):
                    tops[i] = t

    items, ids = [], set()
    for i, q in enumerate(rects):
        top, r = tops[i], res[i]
        if r:
            cid, verified = scryfall.pick_printing(r[0], *(r[2] or (None, None)))
            items.append(("title", i, cid, r[1], verified, r[0]))
            ids.add(cid)
        elif top:
            margin = top[0][1] - (top[1][1] if len(top) > 1 else 0.0)
            if top[0][1] >= art.ART_MIN_SIM and margin >= art.ART_MIN_MARGIN:
                items.append(("art", i, top[0][0], round(top[0][1] * 100, 1), False, None))
                ids.add(top[0][0])
            elif top[0][1] >= art.ART_SUGGEST_MIN:
                items.append(("sugg", i, None, 0, False, None))
        if top:
            ids.update(cid for cid, _ in top)
    info = scryfall.card_info(ids)

    def bbox(q):
        x0, y0 = q.min(axis=0)
        x1, y1 = q.max(axis=0)
        return [round(float(x0)), round(float(y0)), round(float(x1 - x0)), round(float(y1 - y0))]

    dets, suggestions = [], []
    for kind, i, cid, score, verified, key in items:
        if kind == "sugg":
            suggestions.append({"box": bbox(rects[i]), "cands": [
                {"card_id": c, "name": info[c]["name"], "set_code": info[c]["set_code"],
                 "collector_number": info[c]["collector_number"], "image_url": info[c]["image_url"],
                 "sim": round(s, 2)} for c, s in tops[i] if c in info]})
            continue
        c = info.get(cid)
        if not c:
            continue
        if kind == "title" and score < 100 and tops[i]:  # the artwork agrees with the title: more certain
            if any(info.get(t, {}).get("name") == c["name"] and s >= 0.6 for t, s in tops[i][:2]):
                score = max(score, 98.0)
        d = {"card_id": cid, "name": c["name"], "set_code": c["set_code"], "set_name": c["set_name"],
             "collector_number": c["collector_number"], "image_url": c["image_url"], "score": round(score, 1),
             "verified": verified, "ocr_text": "", "key": key, "oracle": c.get("oracle_id"), "box": bbox(rects[i])}
        if kind == "art":
            d["method"] = "art"
        dets.append(d)
    return {"width": W0, "height": H0, "detections": dets, "elapsed": round(time.time() - t0, 2),
            "mode": "instant", "rects": len(rects), "suggestions": suggestions, "art_found": 0, "text_found": 0,
            "art_index": art.size(), "art_total": art.STATE["total"]}
