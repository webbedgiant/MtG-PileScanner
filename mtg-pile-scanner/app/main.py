import hashlib, json, os, subprocess, sys, threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

import time
from rapidfuzz import fuzz, process
from . import art, collection, db, export, phone, scanner, scryfall

STATIC = Path(__file__).parent / "static"
LAST_EXPORT = {"ok": None, "message": "No export yet", "time": None}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def auto_export():
    path = export.export_path()
    try:
        export.write_xlsx(path)
        LAST_EXPORT.update(ok=True, message=f"Spreadsheet updated: {path}", time=now())
    except Exception as e:
        LAST_EXPORT.update(ok=False, time=now(),
                           message=f"Could not write {path}: {e}. If it is open in Excel, close it; "
                                   "it will be rewritten on the next change.")


def background():
    while True:
        scryfall.update_bulk()
        art.build_index()
        time.sleep(6 * 3600)


def update_all():
    scryfall.update_bulk(force=True)
    art.build_index()


@asynccontextmanager
async def lifespan(app):
    db.get()
    scryfall.rebuild_index()
    art.load_matrix()
    try:
        export.backup()
    except Exception:
        pass
    threading.Thread(target=background, daemon=True).start()
    yield


app = FastAPI(title="MTG Pile Scanner", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    with db.LOCK:
        r = db.get().execute("SELECT COUNT(*) u, COALESCE(SUM(qty),0) t FROM collection").fetchone()
    return {"ready": scryfall.ready(), "cards": scryfall.card_count(), "bulk": db.meta_get("bulk_updated_at"),
            "update": scryfall.STATE, "export": LAST_EXPORT, "export_path": str(export.export_path()),
            "unique": r["u"], "total": r["t"],
            "art": {**art.STATE, "size": art.size()}, "can_undo": collection.can_undo(),
            "data_dir": str(db.DATA_DIR)}


@app.post("/api/update")
def update():
    threading.Thread(target=update_all, daemon=True).start()
    return {"ok": True}


PHONE_PATHS = {"/phone", "/api/phone/frame", "/api/phone/photo", "/api/phone/ping"}


@app.middleware("http")
async def lan_guard(request: Request, call_next):
    """The server listens on the LAN so the iPhone can reach it, but only the phone pages (which also need the
    secret token) are reachable from other devices. Everything else is localhost-only."""
    host = request.client.host if request.client else ""
    if not (host in ("::1", "localhost") or host.endswith("127.0.0.1")) and request.url.path not in PHONE_PATHS:
        return JSONResponse({"detail": "Local access only"}, status_code=403)
    return await call_next(request)


def _need_token(t):
    if not phone.check(t):
        raise HTTPException(403, "Invalid or expired link. Scan the QR code in the desktop app again.")


@app.get("/phone")
def phone_page(t: str = ""):
    _need_token(t)
    html = (STATIC / "phone.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__HTTPS_URL__", json.dumps(phone.urls()[1] or "")))


@app.post("/api/phone/ping")
def phone_ping(t: str = ""):
    _need_token(t)
    phone.touch()
    return {"ok": True}


@app.post("/api/phone/frame")
async def phone_frame(request: Request, t: str = ""):
    _need_token(t)
    body = await request.body()
    if not body or len(body) > 25_000_000:
        raise HTTPException(400, "Bad image")
    phone.set_frame(body)
    return {"ok": True}


@app.post("/api/phone/photo")
def phone_photo(file: UploadFile = File(...), t: str = ""):
    _need_token(t)
    data = file.file.read()
    if not data or len(data) > 40_000_000:
        raise HTTPException(400, "Bad image")
    return {"ok": True, "id": phone.add_photo(data)}


@app.get("/api/phone/info")
def phone_info():
    http, https = phone.urls()
    return {"http": http, "https": https, "https_error": phone.STATE["https_error"], "qr_http": phone.qr_svg(http),
            "qr_https": phone.qr_svg(https) if https else ""}


@app.get("/api/phone/poll")
def phone_poll():
    return phone.poll()


@app.get("/api/phone/next")
def phone_next(after: int = 0):
    """Long-poll: returns the instant a frame newer than `after` arrives (push-like, no polling delay)."""
    seq, b = phone.wait_frame(after)
    if b is None:
        return Response(status_code=204, headers={"X-Seq": str(seq), "Cache-Control": "no-store"})
    return Response(b, media_type="image/jpeg", headers={"X-Seq": str(seq), "Cache-Control": "no-store"})


@app.get("/api/phone/frame.jpg")
def phone_frame_get():
    b = phone.STATE["frame"]
    if not b:
        raise HTTPException(404, "No frame yet")
    return Response(b, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/phone/photo/{pid}")
def phone_photo_get(pid: int):
    b = phone.pop_photo(pid)
    if b is None:
        raise HTTPException(404, "Photo already collected")
    return Response(b, media_type="image/jpeg")


@app.get("/api/art/export")
def art_export():
    p = db.DATA_DIR / "art_index_export.npz"
    art.export_index(p)
    return FileResponse(p, filename="mtg_art_index.npz", media_type="application/octet-stream")


@app.post("/api/art/import")
def art_import(file: UploadFile = File(...)):
    p = db.DATA_DIR / "art_index_import.npz"
    with open(p, "wb") as f:
        f.write(file.file.read())
    try:
        n = art.import_index(p)
    except Exception as e:
        raise HTTPException(400, f"Could not import: {e}")
    finally:
        p.unlink(missing_ok=True)
    return {"ok": True, "entries": n}


@app.post("/api/art/start")
def art_start():
    threading.Thread(target=art.build_index, daemon=True).start()
    return {"ok": True}


@app.post("/api/art/stop")
def art_stop():
    art.STATE["cancel"] = True
    return {"ok": True}


SCAN_CACHE = {}


@app.post("/api/scan")
def scan(file: UploadFile = File(...), thorough: bool = Form(False), use_art: bool = Form(True),
         fast: bool = Form(False), mode: str = Form("full")):
    if not scryfall.ready():
        raise HTTPException(503, "The card database is still downloading. Try again in a minute.")
    data = file.file.read()
    key = (hashlib.sha1(data).hexdigest(), thorough, use_art, fast, mode, art.size() // 500)
    if key in SCAN_CACHE:  # identical image + settings: answer instantly
        return {**SCAN_CACHE[key], "cached": True}
    img = scanner.decode(data)
    if img is None:
        raise HTTPException(400, "Could not read that image.")
    res = scanner.scan_instant(img, use_art) if mode == "instant" else scanner.scan(img, thorough, use_art, fast)
    SCAN_CACHE[key] = res
    while len(SCAN_CACHE) > 8:
        SCAN_CACHE.pop(next(iter(SCAN_CACHE)))
    return res


@app.get("/api/search")
def search(q: str):
    """Manual add: prefix + fuzzy search over card names."""
    qn = scryfall.norm(q)
    if len(qn) < 2 or not scryfall.ready():
        return []
    names = scryfall.INDEX["names"]
    keys = [n for n in names if n.startswith(qn)][:8]
    if len(keys) < 8:
        for n, _, _ in process.extract(qn, names, scorer=fuzz.WRatio, limit=12, score_cutoff=72):
            if n not in keys:
                keys.append(n)
    keys = keys[:8]
    ids = [scryfall.INDEX["by_name"][k][0][0] for k in keys]
    info = scryfall.card_info(ids)
    return [{"card_id": i, "name": info[i]["name"], "set_code": info[i]["set_code"],
             "collector_number": info[i]["collector_number"], "image_url": info[i]["image_url"]}
            for i in ids if i in info]


@app.post("/api/open-folder")
def open_folder():
    p = str(db.DATA_DIR)
    if sys.platform == "win32":
        os.startfile(p)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
    return {"ok": True}


@app.post("/api/quit")
def quit_app():
    threading.Timer(0.4, lambda: os._exit(0)).start()
    return {"ok": True}


@app.get("/api/printings")
def printings(card_id: str):
    with db.LOCK:
        c = db.get().execute("SELECT oracle_id, name FROM cards WHERE id=?", (card_id,)).fetchone()
        if not c:
            raise HTTPException(404, "Unknown card")
        if c["oracle_id"]:
            rows = db.get().execute("SELECT * FROM cards WHERE oracle_id=? ORDER BY released_at DESC",
                                    (c["oracle_id"],)).fetchall()
        else:
            rows = db.get().execute("SELECT * FROM cards WHERE name=? ORDER BY released_at DESC",
                                    (c["name"],)).fetchall()
    return [dict(r) for r in rows]


class Item(BaseModel):
    card_id: str
    qty: int = 1
    foil: int = 0


class AddBody(BaseModel):
    items: List[Item]


@app.post("/api/collection/add")
def add(body: AddBody):
    n = collection.add([i.model_dump() for i in body.items])
    auto_export()
    return {"ok": True, "added": n, "export": LAST_EXPORT}


@app.post("/api/collection/undo")
def undo():
    n = collection.undo()
    auto_export()
    return {"ok": True, "removed": n, "export": LAST_EXPORT}


@app.post("/api/collection/set")
def set_qty(item: Item):
    with db.LOCK:
        c = db.get()
        if item.qty <= 0:
            c.execute("DELETE FROM collection WHERE card_id=? AND foil=?", (item.card_id, item.foil))
        else:
            c.execute("UPDATE collection SET qty=?, updated_at=? WHERE card_id=? AND foil=?",
                      (item.qty, now(), item.card_id, item.foil))
    auto_export()
    return {"ok": True, "export": LAST_EXPORT}


@app.post("/api/collection/clear")
def clear():
    with db.LOCK:
        db.get().execute("DELETE FROM collection")
    auto_export()
    return {"ok": True}


@app.get("/api/collection/table")
def table():
    keys, rows = export.fetch()
    return {"columns": export.COLUMNS, "keys": keys, "rows": rows}


class SettingsBody(BaseModel):
    export_path: str


@app.post("/api/settings")
def settings(body: SettingsBody):
    p = body.export_path.strip().strip('"')
    if p and not p.lower().endswith(".xlsx"):
        raise HTTPException(400, "Export path must end in .xlsx")
    db.meta_set("setting:export_path", p)
    auto_export()
    return {"export_path": str(export.export_path()), "export": LAST_EXPORT}


@app.get("/api/export.csv")
def download_csv():
    return Response(export.csv_bytes(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="mtg_collection.csv"'})


@app.get("/api/export.xlsx")
def download():
    return Response(export.xlsx_bytes(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="mtg_collection.xlsx"'})
