import hashlib, os, subprocess, sys, threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import time
from rapidfuzz import fuzz, process
from . import art, collection, db, export, scanner, scryfall

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
         fast: bool = Form(False)):
    if not scryfall.ready():
        raise HTTPException(503, "The card database is still downloading. Try again in a minute.")
    data = file.file.read()
    key = (hashlib.sha1(data).hexdigest(), thorough, use_art, fast, art.size() // 500)
    if key in SCAN_CACHE:  # identical image + settings: answer instantly
        return {**SCAN_CACHE[key], "cached": True}
    img = scanner.decode(data)
    if img is None:
        raise HTTPException(400, "Could not read that image.")
    res = scanner.scan(img, thorough, use_art, fast)
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
