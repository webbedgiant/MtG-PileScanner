import os, sys, traceback, platform
out = []
def log(*a):
    line = " ".join(str(x) for x in a); print(line); out.append(line)
def step(name, fn):
    try:
        log(f"[ OK ] {name}: {fn()}")
    except Exception:
        log(f"[FAIL] {name}:\n{traceback.format_exc()}")
log("Python", sys.version, platform.platform())
try:
    import truststore; truststore.inject_into_ssl(); log("truststore active")
except Exception as e:
    log("truststore not active:", e)
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import db
DATA = db.DATA_DIR
H = {"User-Agent": "MTGPileScanner/1.0", "Accept": "application/json;q=0.9,*/*;q=0.8"}
info = {}
def bulk():
    r = requests.get("https://api.scryfall.com/bulk-data", headers=H, timeout=30)
    r.raise_for_status()
    from app import scryfall
    e = scryfall._find_entry(r.json()["data"])
    info["uri"] = scryfall._download_uri(e); return f"{r.status_code}, entry fields {sorted(e)}"
step("api.scryfall.com bulk-data", bulk)
def head():
    r = requests.get(info["uri"], headers=H, stream=True, timeout=30)
    n = next(r.iter_content(1024)); r.close(); return f"{r.status_code}, got {len(n)} bytes from {info['uri']}"
if "uri" in info: step("data.scryfall.io download", head)
def imports():
    import ijson, rapidfuzz, cv2, openpyxl, numpy
    return f"ijson backend={ijson.backend}, cv2={cv2.__version__}"
step("python packages", imports)
def ocr():
    try:
        from rapidocr import RapidOCR
    except ImportError:
        from rapidocr_onnxruntime import RapidOCR
    RapidOCR(); return "OCR engine loads"
step("OCR engine", ocr)
def data():
    p = str(DATA)
    os.makedirs(p, exist_ok=True); open(os.path.join(p, "t"), "w").write("x"); os.remove(os.path.join(p, "t")); return p
step("data folder writable", data)
p = os.path.join(str(DATA), "app.log")
if os.path.exists(p):
    log("--- last lines of app.log ---"); log("".join(open(p, encoding="utf-8", errors="replace").readlines()[-25:]))
open("diagnose.txt", "w", encoding="utf-8").write("\n".join(out))
print("\nSaved to diagnose.txt - paste its contents into the chat.")
