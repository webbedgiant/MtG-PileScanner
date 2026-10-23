"""SQLite persistence. Data lives in %APPDATA%\\MTGPileScanner so it survives restarts/updates."""
import os, sqlite3, sys, threading
from pathlib import Path

APP_NAME = "MTGPileScanner"


def _default_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


DATA_DIR = Path(os.environ.get("MTG_SCANNER_DATA") or _default_dir())
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "scanner.db"
LOCK = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, oracle_id TEXT, name TEXT, set_code TEXT, set_name TEXT,
  collector_number TEXT, rarity TEXT, released_at TEXT, mana_cost TEXT, cmc REAL,
  type_line TEXT, colors TEXT, power TEXT, toughness TEXT, image_url TEXT,
  scryfall_uri TEXT, price_usd REAL, price_usd_foil REAL, promo INTEGER);
CREATE INDEX IF NOT EXISTS idx_cards_oracle ON cards(oracle_id);
CREATE TABLE IF NOT EXISTS collection(
  card_id TEXT, foil INTEGER DEFAULT 0, qty INTEGER NOT NULL,
  added_at TEXT, updated_at TEXT, PRIMARY KEY(card_id, foil));
CREATE TABLE IF NOT EXISTS art(illustration_id TEXT PRIMARY KEY, card_id TEXT, desc BLOB);
CREATE TABLE IF NOT EXISTS oracle(oracle_id TEXT PRIMARY KEY, name TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

def get():
    global _conn
    with LOCK:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=60000")
            _conn.executescript(SCHEMA)
            if "illustration_id" not in {r[1] for r in _conn.execute("PRAGMA table_info(cards)")}:
                _conn.execute("ALTER TABLE cards ADD COLUMN illustration_id TEXT")
            if "set_type" not in {r[1] for r in _conn.execute("PRAGMA table_info(cards)")}:
                _conn.execute("ALTER TABLE cards ADD COLUMN set_type TEXT")
        return _conn

def meta_get(key, default=None):
    with LOCK:
        r = get().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def meta_set(key, value):
    with LOCK:
        get().execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))
