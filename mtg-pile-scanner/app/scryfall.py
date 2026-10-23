"""Scryfall bulk-data sync (keeps every set up to date) + fuzzy card-name matching."""
import gzip, json, logging, re, sqlite3, threading, time, unicodedata
import requests, ijson
from rapidfuzz import fuzz, process
from . import db, net

log = logging.getLogger("mtg")
log.setLevel(logging.INFO)
log.addHandler(logging.FileHandler(db.DATA_DIR / "app.log", encoding="utf-8"))
HEADERS = {"User-Agent": "MTGPileScanner/1.0", "Accept": "application/json;q=0.9,*/*;q=0.8"}
BULK_URL = "https://api.scryfall.com/bulk-data"
SKIP_LAYOUTS = {"token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar"}
SKIP_SET_TYPES = {"token", "memorabilia"}
# Rules-text keywords that are also (rare) card names; ignored as single-line OCR hits. Tweak freely.
KEYWORD_STOP = {"flash", "flying", "haste", "trample", "vigilance", "deathtouch", "lifelink", "reach",
                "menace", "defender", "fear", "flanking", "shadow", "hexproof", "indestructible", "wither"}
BAD_SET_TYPES = {"funny", "promo", "memorabilia", "token", "minigame", "treasure_chest", "vanguard", "alchemy", "box"}
STATE = {"updating": False, "message": "", "progress": 0.0, "error": None}
INDEX = {"names": [], "by_name": {}, "sets": set()}
COLS = ["id", "oracle_id", "name", "set_code", "set_name", "collector_number", "rarity", "released_at",
        "mana_cost", "cmc", "type_line", "colors", "power", "toughness", "image_url", "scryfall_uri",
        "price_usd", "price_usd_foil", "promo", "illustration_id", "set_type"]
INSERT = f"INSERT OR REPLACE INTO cards ({','.join(COLS)}) VALUES ({','.join(':' + c for c in COLS)})"


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def keep(c):
    return (c.get("lang") == "en" and not c.get("digital") and c.get("layout") not in SKIP_LAYOUTS
            and c.get("set_type") not in SKIP_SET_TYPES)


def to_row(c):
    faces = c.get("card_faces") or []
    f0 = faces[0] if faces else {}
    imgs = c.get("image_uris") or f0.get("image_uris") or {}
    prices = c.get("prices") or {}
    colors = c.get("colors")
    if colors is None:
        colors = f0.get("colors") or []
    return {
        "id": c["id"], "oracle_id": c.get("oracle_id") or f0.get("oracle_id"), "name": c["name"],
        "set_code": c.get("set", ""), "set_name": c.get("set_name", ""),
        "collector_number": str(c.get("collector_number", "")), "rarity": c.get("rarity", ""),
        "released_at": c.get("released_at", ""), "mana_cost": c.get("mana_cost") or f0.get("mana_cost") or "",
        "cmc": float(c.get("cmc") or 0), "type_line": c.get("type_line") or f0.get("type_line") or "",
        "colors": "".join(colors), "power": c.get("power") or f0.get("power") or "",
        "toughness": c.get("toughness") or f0.get("toughness") or "",
        "image_url": imgs.get("small", ""), "scryfall_uri": c.get("scryfall_uri", ""),
        "price_usd": _f(prices.get("usd")), "price_usd_foil": _f(prices.get("usd_foil")),
        "promo": 1 if c.get("promo") else 0,
        "illustration_id": c.get("illustration_id") or f0.get("illustration_id"),
        "set_type": c.get("set_type", "")}


def rebuild_index():
    with db.LOCK:
        rows = db.get().execute(
            "SELECT id,name,set_code,collector_number,released_at,promo,set_type FROM cards").fetchall()
    by_name, sets = {}, set()
    for r in rows:
        sets.add(r["set_code"])
        for face in r["name"].split(" // "):
            by_name.setdefault(norm(face), []).append(
                (r["id"], r["set_code"], r["collector_number"], r["released_at"] or "", r["promo"], r["set_type"] or ""))
    for v in by_name.values():  # default printing = newest non-promo
        v.sort(key=lambda t: t[3], reverse=True)
        v.sort(key=lambda t: (t[4], t[5] in BAD_SET_TYPES))
    INDEX.update(names=list(by_name.keys()), by_name=by_name, sets=sets)
    try:
        from . import textindex
        textindex.build()
    except Exception:
        log.exception("rules-text index failed")


def ready():
    return bool(INDEX["names"])


def card_count():
    with db.LOCK:
        return db.get().execute("SELECT COUNT(*) n FROM cards").fetchone()["n"]


MANUAL_FILE = db.DATA_DIR / "manual_default_cards.json"


def _find_entry(items):
    for e in items:
        if e.get("type") == "default_cards":
            return e
    for e in items:
        if "default" in str(e.get("name", "")).lower() and "card" in str(e.get("name", "")).lower():
            return e
    raise RuntimeError("Scryfall bulk-data list has no 'Default Cards' entry. Entries: "
                       + ", ".join(str(e.get("type") or e.get("name")) for e in items))


def _download_uri(entry, depth=0):
    for k in ("download_uri", "download_url", "downloadUri", "download"):
        if isinstance(entry.get(k), str) and entry[k].startswith("http"):
            return entry[k]
    for k, v in entry.items():
        if "download" in k.lower() and isinstance(v, str) and v.startswith("http"):
            return v
    if depth == 0 and isinstance(entry.get("uri"), str):  # the entry's own API page usually has the link
        r = net.get(entry["uri"], headers=HEADERS, timeout=30)
        r.raise_for_status()
        return _download_uri(r.json(), 1)
    raise RuntimeError(f"Scryfall bulk entry has no download link. Fields present: {sorted(entry)}")


def _import_file(path):
    """Import a Scryfall default-cards file (JSON array or JSON Lines, gzip or plain)."""
    size = path.stat().st_size or 1
    conn = sqlite3.connect(db.DB_PATH, isolation_level=None, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        batch, obatch, seen_oracle, n = [], [], set(), 0

        def flush():
            nonlocal batch, obatch
            if batch or obatch:
                conn.execute("BEGIN")
                if batch:
                    conn.executemany(INSERT, batch)
                if obatch:
                    conn.executemany("INSERT OR REPLACE INTO oracle(oracle_id,name,text) VALUES(?,?,?)", obatch)
                conn.execute("COMMIT")
                batch, obatch = [], []

        with open(path, "rb") as raw:
            gz = raw.read(2) == b"\x1f\x8b"
            raw.seek(0)
            f = gzip.GzipFile(fileobj=raw) if gz else raw
            first = f.peek(64).lstrip()[:1]  # '[' = JSON array, '{' = JSON Lines
            cards = ijson.items(f, "item") if first == b"[" else (json.loads(l) for l in f if l.strip())
            for c in cards:
                if keep(c):
                    batch.append(to_row(c))
                    n += 1
                    faces = c.get("card_faces") or []
                    oid = c.get("oracle_id") or (faces[0].get("oracle_id") if faces else None)
                    if oid and oid not in seen_oracle:
                        seen_oracle.add(oid)
                        text = c.get("oracle_text") or "\n".join(fc.get("oracle_text") or "" for fc in faces)
                        obatch.append((oid, c["name"], text))
                if len(batch) >= 3000:
                    flush()
                    STATE["progress"] = 0.4 + 0.6 * min(raw.tell() / size, 1.0)
                    STATE["message"] = f"Importing cards… {n:,}"
        flush()
    finally:
        conn.close()


def update_bulk(force=False):
    if STATE["updating"]:
        return
    STATE.update(updating=True, error=None, message="Checking Scryfall for new sets…", progress=0.0)
    tmp = db.DATA_DIR / "default_cards.json"
    try:
        if MANUAL_FILE.exists():  # offline workaround: user-supplied file
            STATE["message"] = "Importing manually supplied card file…"
            _import_file(MANUAL_FILE)
            MANUAL_FILE.rename(MANUAL_FILE.with_suffix(".imported"))
            rebuild_index()
            STATE["message"] = f"Card database imported ({card_count():,} printings)"
            return
        try:
            r = net.get(BULK_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Could not reach api.scryfall.com ({type(e).__name__}: {e}). "
                               "Check internet/VPN/proxy/firewall/antivirus.") from e
        entry = _find_entry(r.json()["data"])
        dl_uri = _download_uri(entry)
        version = str(entry.get("updated_at") or dl_uri)
        if (not force and db.meta_get("bulk_updated_at") == version and card_count() > 0
                and db.meta_get("cards_schema") == "4"):
            STATE["message"] = "Card database is up to date"
            return
        STATE["message"] = "Downloading latest card data from Scryfall…"
        log.info("Downloading %s", dl_uri)
        try:
            with net.get(dl_uri, headers=HEADERS, stream=True, timeout=(30, 120)) as resp:
                resp.raise_for_status()
                total, done = entry.get("size") or 0, 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            STATE["progress"] = min(done / total, 1.0) * 0.4
        except Exception as e:
            raise RuntimeError(f"Could not download card file from {dl_uri} "
                               f"({type(e).__name__}: {e}). Manual workaround: download it in your browser, "
                               f"save it as {MANUAL_FILE} and click Retry.") from e
        STATE["message"] = "Importing cards (first run takes a few minutes)…"
        _import_file(tmp)
        db.meta_set("bulk_updated_at", version)
        db.meta_set("cards_schema", "4")
        rebuild_index()
        STATE["message"] = f"Card database updated ({card_count():,} printings)"
        log.info(STATE["message"])
    except Exception as e:  # keep using whatever data we already have
        log.exception("Card data update failed")
        STATE["error"] = f"{type(e).__name__}: {e}"
        STATE["message"] = "Update failed; using existing card data" if ready() else "Card data download failed"
    finally:
        STATE.update(updating=False, progress=1.0)
        try:
            tmp.unlink()
        except OSError:
            pass


def background_loop(hours=6):
    while True:
        update_bulk()
        time.sleep(hours * 3600)


def match_text(text):
    """Return (name_key, score) for an OCR line, or None."""
    t = norm(text)
    if len(t) < 3 or len(t) > 45:
        return None
    by_name, names = INDEX["by_name"], INDEX["names"]
    cands = [t]
    toks = t.split()
    for k in (1, 2):  # trailing mana-cost junk such as "1u" or "2 b"
        if len(toks) > k and all(len(x) <= 3 for x in toks[-k:]):
            cands.append(" ".join(toks[:-k]))
    best = None
    for c in cands:
        if len(c) < 3:
            continue
        if c in by_name:
            best = (c, 100.0)
            break
        res = process.extractOne(c, names, scorer=fuzz.ratio, score_cutoff=84)
        if res and (res[1] >= 92 or (res[1] >= 86 and len(res[0]) >= 14)) and (best is None or res[1] > best[1]):
            best = (res[0], res[1])
    if not best:
        return None
    if len(best[0]) < 5 and best[1] < 100:
        return None
    if best[0] in KEYWORD_STOP:
        return None
    return best


def pick_printing(key, set_hint=None, num_hint=None):
    """Choose a printing; returns (card_id, verified)."""
    prints = INDEX["by_name"][key]
    if set_hint:
        cand = [p for p in prints if p[1] == set_hint]
        if num_hint:
            exact = [p for p in cand if p[2].lstrip("0") == num_hint.lstrip("0")]
            if exact:
                return exact[0][0], True
        if cand:
            return cand[0][0], True
    return prints[0][0], len({p[1] for p in prints}) == 1


def card_info(ids):
    out, ids = {}, list(ids)
    with db.LOCK:
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            q = f"SELECT * FROM cards WHERE id IN ({','.join('?' * len(part))})"
            for r in db.get().execute(q, part):
                out[r["id"]] = dict(r)
    return out
