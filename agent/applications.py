#!/usr/bin/env python3
"""Log de candidaturas, no mesmo formato da planilha "Application Spreadsheet - the candidate"
(aba "Creating a Target Company List"). O agente roda em segundo plano e não fala com o
Google Sheets; a ponte é copiar as linhas novas e colar na planilha."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

SEED = Path(__file__).resolve().parent / "data" / "applications_seed.json"
COLS = ["company", "job_title", "app_date", "job_url", "status", "contact", "contact_role",
        "contact_method", "follow_up", "next_steps", "outcome"]
HEAD = ["Company Name", "Job Title", "Application Date", "Job Description Link", "Status",
        "Contact Person", "Contact's Role", "Contact Method", "Follow-up Date", "Next Steps", "Outcome"]
STATUSES = ["applied", "hunting", "interview scheduled", "rejected", "offer"]
OUTCOMES = ["Awaiting Response", "Interview Scheduled", "Continue Applying", "Offer", "Rejected"]


def init(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS applications (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, synced INTEGER DEFAULT 0,
      company TEXT, job_title TEXT, app_date TEXT, job_url TEXT, status TEXT,
      contact TEXT, contact_role TEXT, contact_method TEXT, follow_up TEXT,
      next_steps TEXT, outcome TEXT);
    """)
    if con.execute("SELECT count(*) FROM applications").fetchone()[0] == 0 and SEED.exists():
        for r in json.loads(SEED.read_text()):
            con.execute("INSERT INTO applications (created_at, synced, " + ",".join(COLS) + ") VALUES (?,1,"
                        + ",".join("?" * len(COLS)) + ")", ("seed", *[r.get(c, "") for c in COLS]))


def _db():
    from agent import db
    return db()


def _now():
    from agent import now
    return now()


def _rows(sql, args=()):
    with _db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_TRACK = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "rcm", "from", "trk"}


def _ukey(url):
    """Link sem parâmetros de rastreio, pra casar a mesma vaga mesmo com ?utm… a mais ou a menos."""
    import urllib.parse as up
    u = up.urlparse((url or "").strip())
    q = [(k, v) for k, v in up.parse_qsl(u.query) if k not in _TRACK]
    return (u.netloc + u.path.rstrip("/") + ("?" + up.urlencode(q) if q else "")).lower()


def _job_id(url):
    m = re.search(r"(\d{7,})", url or "")
    return m.group(1) if m else ""


def applied_index():
    """Empresas e vagas que já têm candidatura (status applied em diante), para marcar no radar."""
    by_url, by_pair = set(), set()
    for r in _rows("SELECT company, job_title, job_url FROM applications WHERE status NOT IN ('', 'hunting')"):
        if _job_id(r["job_url"]):
            by_url.add(_job_id(r["job_url"]))
        by_pair.add((_norm(r["company"]), _norm(r["job_title"])))
    return by_url, by_pair


def list_all():
    return _rows("SELECT * FROM applications ORDER BY id DESC")


def contact_for(company):
    """Pessoa com quem você já falou nessa empresa (fila de conexões), se houver."""
    rows = _rows("SELECT person, person_title, profile_url, status, invited_at FROM connections"
                 " WHERE lower(company)=lower(?) AND profile_url IS NOT NULL AND status IN"
                 " ('invited','accepted','messaged') ORDER BY invited_at DESC LIMIT 1", (company,))
    return rows[0] if rows else None


def add(data):
    company = (data.get("company") or "").strip()
    if not company:
        raise ValueError("Falta o nome da empresa.")
    today = _now()
    row = {c: (data.get(c) or "").strip() if isinstance(data.get(c), str) else (data.get(c) or "") for c in COLS}
    row["company"] = company
    row["status"] = row["status"] or "applied"
    row["app_date"] = row["app_date"] or today.strftime("%d/%m/%Y")
    row["outcome"] = row["outcome"] or "Awaiting Response"
    c = contact_for(company)
    if c and not row["contact"]:
        row["contact"] = c["profile_url"]
        row["contact_role"] = row["contact_role"] or (c["person_title"] or "")
        row["contact_method"] = row["contact_method"] or "Linkedin"
    if row["contact"] and not row["follow_up"]:
        row["follow_up"] = (today + timedelta(days=7)).strftime("%d/%m/%Y")
        row["next_steps"] = row["next_steps"] or "Send follow-up email"
    with _db() as con:
        cur = con.execute("INSERT INTO applications (created_at, synced, " + ",".join(COLS) + ") VALUES (?,0,"
                          + ",".join("?" * len(COLS)) + ")", (today.isoformat(), *[row[c] for c in COLS]))
    push_async()
    return cur.lastrowid


def update(aid, data):
    fields = {k: data[k] for k in COLS if k in data}
    if not fields:
        return
    with _db() as con:
        con.execute("UPDATE applications SET synced=0, " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                    (*fields.values(), aid))
    push_async()


def delete(aid):
    with _db() as con:
        con.execute("DELETE FROM applications WHERE id=?", (aid,))


def _cell(v):
    return re.sub(r"[\t\r\n]+", " ", str(v or "")).strip()


def tsv_pending(mark=False):
    """Linhas ainda não coladas na planilha, em ordem cronológica, prontas pra colar (sem cabeçalho)."""
    rows = _rows("SELECT * FROM applications WHERE synced=0 ORDER BY id")
    if mark and rows:
        with _db() as con:
            con.execute("UPDATE applications SET synced=1 WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
    return "\n".join("\t".join(_cell(r[c]) for c in COLS) for r in rows), len(rows)


# ---------------------------------------------------------------- Google Sheets (Apps Script)
SYNC_FILE = Path(__file__).resolve().parent / "data" / "sheet_sync.json"
_lock = threading.Lock()


def sheet_cfg():
    try:
        return json.loads(SYNC_FILE.read_text())
    except Exception:
        return {}


def save_url(url):
    url = (url or "").strip()
    if url and not re.match(r"^https://script\.google\.com/macros/s/[\w-]+/exec$", url):
        raise ValueError("Cole a URL do app da web que termina em /exec.")
    c = sheet_cfg()
    c["url"] = url
    SYNC_FILE.write_text(json.dumps(c))
    SYNC_FILE.chmod(0o600)


def sheet_status():
    from agent import meta_get
    c = sheet_cfg()
    return {"connected": bool(c.get("url")), "last": meta_get("sheet_last", ""), "error": meta_get("sheet_error", "")}


def _call(payload):
    c = sheet_cfg()
    if not c.get("url"):
        raise RuntimeError("Planilha não conectada.")
    req = urllib.request.Request(c["url"], data=json.dumps({"token": c.get("token"), **payload}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    last = "resposta vazia"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:    # o 302 do Apps Script vira GET e traz o JSON
                raw = r.read().decode("utf-8", "replace")
            out = json.loads(raw)
            break
        except ValueError:
            last = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style).*?</\1>", "", raw, flags=re.S))).strip()[:160] or last
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)[:160]
        time.sleep(1.5)
    else:
        raise RuntimeError("A planilha não respondeu: " + last)
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "erro na planilha")
    return out


def _result(err=None):
    from agent import meta_set
    meta_set("sheet_error", err or "")
    if not err:
        meta_set("sheet_last", _now().isoformat())


def push():
    """Manda as linhas pendentes. Só marca como sincronizada o que a planilha confirmou."""
    if not sheet_cfg().get("url"):
        return 0
    with _lock:
        rows = _rows("SELECT * FROM applications WHERE synced=0 ORDER BY id")
        if not rows:
            return 0
        try:
            res = _call({"action": "upsert", "rows": [[_cell(r[c]) for c in COLS] for r in rows]})
        except Exception as e:
            _result(str(e)[:200])
            raise
        with _db() as con:
            con.execute("UPDATE applications SET synced=1 WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
        _result()
        return len(res.get("result", []))


def pull():
    """A planilha é a raiz: o que foi editado lá substitui o local (menos o que ainda está pendente de envio)."""
    out = _call({"action": "pull"})
    local = {(_norm(r["company"]), _norm(r["job_title"]), _ukey(r["job_url"])): r
             for r in _rows("SELECT * FROM applications")}
    added = updated = 0
    with _db() as con:
        for raw in out.get("rows", []):
            row = dict(zip(COLS, [(v or "").strip() for v in (list(raw) + [""] * 11)[:11]]))
            if not row["company"]:
                continue
            k = (_norm(row["company"]), _norm(row["job_title"]), _ukey(row["job_url"]))
            cur = local.get(k)
            if cur is None:
                con.execute("INSERT INTO applications (created_at, synced," + ",".join(COLS) + ") VALUES (?,1,"
                            + ",".join("?" * len(COLS)) + ")", ("sheet", *[row[c] for c in COLS]))
                added += 1
            elif cur["synced"] and any((cur[c] or "") != row[c] for c in COLS):
                con.execute("UPDATE applications SET " + ",".join(f"{c}=?" for c in COLS) + " WHERE id=?",
                            (*[row[c] for c in COLS], cur["id"]))
                updated += 1
    _result()
    return {"added": added, "updated": updated}


def sync():
    n = push()
    return {"pushed": n, **pull()}


def push_async():
    def run():
        try:
            push()
        except Exception as e:
            from agent import log
            log("sheet push falhou:", e)
    threading.Thread(target=run, daemon=True).start()
