"""Collection writes (kept separate from the web layer so they are easy to test)."""
import json
from datetime import datetime, timezone
from . import db


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(items):
    """items: [{card_id, qty, foil}]. Remembers the batch so it can be undone."""
    t, agg = now(), {}
    with db.LOCK:
        c = db.get()
        c.execute("BEGIN")
        try:
            for it in items:
                q, foil = int(it["qty"]), 1 if it.get("foil") else 0
                if q <= 0:
                    continue
                c.execute("""INSERT INTO collection(card_id,foil,qty,added_at,updated_at) VALUES(?,?,?,?,?)
                             ON CONFLICT(card_id,foil) DO UPDATE SET qty=qty+excluded.qty, updated_at=excluded.updated_at""",
                          (it["card_id"], foil, q, t, t))
                agg[(it["card_id"], foil)] = agg.get((it["card_id"], foil), 0) + q
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    db.meta_set("last_add", json.dumps([{"card_id": k[0], "foil": k[1], "qty": v} for k, v in agg.items()]))
    return sum(agg.values())


def can_undo():
    return bool(db.meta_get("last_add"))


def undo():
    raw = db.meta_get("last_add")
    if not raw:
        return 0
    items, t = json.loads(raw), now()
    with db.LOCK:
        c = db.get()
        c.execute("BEGIN")
        for it in items:
            c.execute("UPDATE collection SET qty=qty-?, updated_at=? WHERE card_id=? AND foil=?",
                      (it["qty"], t, it["card_id"], it["foil"]))
        c.execute("DELETE FROM collection WHERE qty<=0")
        c.execute("COMMIT")
    db.meta_set("last_add", "")
    return sum(i["qty"] for i in items)
