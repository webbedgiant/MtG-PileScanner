"""Card-art recognition: a compact visual fingerprint of every card's artwork."""
import sqlite3, time
import cv2, numpy as np, requests
from concurrent.futures import ThreadPoolExecutor
from . import db

ART_BOX = (0.08, 0.92, 0.125, 0.54)   # x0, x1, y0, y1 of the art window as fractions of the card face
ART_MIN_SIM = 0.72                    # minimum similarity to accept an art-only match (tune if needed)
ART_MIN_MARGIN = 0.03                 # required lead over the best *different* card
HEADERS = {"User-Agent": "MTGPileScanner/1.0", "Accept": "image/jpeg,*/*;q=0.8"}
STATE = {"building": False, "cancel": False, "done": 0, "total": 0, "error": None}
DATA = (None, [], [])                 # (matrix, card_ids, names) swapped atomically


def _describe(art_bgr):
    crop = cv2.resize(art_bgr, (24, 18), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = lab[:, :, 0]
    parts = [((L - L.mean()) / (L.std() + 1e-3)).ravel()]
    for ch in (1, 2):
        C = cv2.resize(lab[:, :, ch], (8, 6), interpolation=cv2.INTER_AREA)
        parts.append(((C - C.mean()) / (C.std() + 1e-3) * 0.5).ravel())
    v = np.concatenate(parts).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-6)


def art_descriptor(card_bgr):
    """Fingerprint from a whole (upright) card image."""
    h, w = card_bgr.shape[:2]
    x0, x1, y0, y1 = ART_BOX
    return _describe(card_bgr[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)])


def art_descriptor_from_art(art_bgr, inset=0.03):
    """Fingerprint from an already-isolated art rectangle (upright)."""
    h, w = art_bgr.shape[:2]
    return _describe(art_bgr[int(inset * h):int((1 - inset) * h), int(inset * w):int((1 - inset) * w)])


def load_matrix():
    global DATA
    with db.LOCK:
        rows = db.get().execute(
            "SELECT a.card_id, a.desc, c.name FROM art a JOIN cards c ON c.id = a.card_id").fetchall()
    if not rows:
        DATA = (None, [], [])
        return
    M = np.vstack([np.frombuffer(r["desc"], dtype=np.float16) for r in rows]).astype(np.float32)
    DATA = (M, [r["card_id"] for r in rows], [r["name"] for r in rows])


def _append(rows):  # rows: [(card_id, name, descriptor_bytes)]
    global DATA
    M, ids, names = DATA
    new = np.vstack([np.frombuffer(r[2], dtype=np.float16) for r in rows]).astype(np.float32)
    DATA = (new if M is None else np.vstack([M, new]), ids + [r[0] for r in rows], names + [r[1] for r in rows])


def size():
    return len(DATA[1])


def match(desc):
    """Returns (card_id, similarity, margin_over_other_card) or None."""
    M, ids, names = DATA
    if M is None or len(M) < 50:
        return None
    sims = M @ desc
    k = min(12, len(sims))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    i1 = int(top[0])
    s2 = next((float(sims[j]) for j in top[1:] if names[j] != names[i1]), 0.0)
    return ids[i1], float(sims[i1]), float(sims[i1]) - s2


def _fetch(item):
    ill, cid, url, name = item
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return None
            return ill, cid, name, art_descriptor(img).astype(np.float16).tobytes()
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            time.sleep(0.5 + attempt)
    return None


def build_index(workers=32):
    """Download each distinct artwork once (small card images), keep only the fingerprint. Resumable."""
    if STATE["building"]:
        return
    STATE.update(building=True, cancel=False, error=None)
    ex = None
    try:
        with db.LOCK:
            have = {r[0] for r in db.get().execute("SELECT illustration_id FROM art")}
            rows = db.get().execute(
                """SELECT id, oracle_id, name, illustration_id, image_url FROM cards
                   WHERE illustration_id IS NOT NULL AND image_url != '' ORDER BY released_at DESC""").fetchall()
        seen, per_oracle, todo, total = set(), {}, [], 0
        for r in rows:  # newest printing per artwork; one artwork per card first so every card is covered early
            ill = r["illustration_id"]
            if ill in seen:
                continue
            seen.add(ill)
            total += 1
            if ill in have:
                continue
            rank = per_oracle.get(r["oracle_id"], 0)
            per_oracle[r["oracle_id"]] = rank + 1
            todo.append((rank, (ill, r["id"], r["image_url"], r["name"])))
        todo.sort(key=lambda t: t[0])
        todo = [t[1] for t in todo]
        STATE.update(done=total - len(todo), total=total)
        if size() == 0:
            load_matrix()
        if not todo:
            return
        conn = sqlite3.connect(db.DB_PATH, isolation_level=None, timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        ex = ThreadPoolExecutor(max_workers=workers)
        batch = []

        def flush():
            nonlocal batch
            if batch:
                conn.execute("BEGIN")
                conn.executemany("INSERT OR REPLACE INTO art(illustration_id, card_id, desc) VALUES(?,?,?)",
                                 [(b[0], b[1], b[3]) for b in batch])
                conn.execute("COMMIT")
                _append([(b[1], b[2], b[3]) for b in batch])
                STATE["done"] += len(batch)
                batch = []

        try:
            for res in ex.map(_fetch, todo):  # all downloads run concurrently; results arrive in order
                if STATE["cancel"]:
                    break
                if res:
                    batch.append(res)
                if len(batch) >= 400:
                    flush()
            flush()
        finally:
            conn.close()
    except Exception as e:
        STATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        if ex:
            ex.shutdown(wait=False, cancel_futures=True)
        STATE["building"] = False
