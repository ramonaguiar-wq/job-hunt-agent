#!/usr/bin/env python3
""""Quero aplicar" (revisão 2026-10-16, Parte 5): pessoas certas pra conectar em cada vaga marcada.

- Marcar a vaga (tecla S no painel) guarda a vaga em `job_wants`.
- "Buscar pessoas" faz até 3 buscas no DuckDuckGo (recrutador, hiring manager, designer do time) e guarda
  até 5 perfis em `connections` (why='quero-aplicar', status 'queued' = a conectar). Só vale link de perfil
  que apareceu no resultado da busca: nunca abre linkedin.com e nunca monta URL. Não usa Claude (sem cota).
- Você também pode colar um perfil na mão.
- Nota de convite em inglês, até 300 caracteres, só com fatos que já estão no connect.py.
- Status: a conectar -> enviado por mim -> aceitou -> respondeu. Nada é enviado pelo agente: você copia e envia.
"""
import html as htmlmod
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

MAX_PEOPLE = 5
FOLLOW_UP = {"invited": 7, "accepted": 3, "messaged": 7}  # dias sem novidade até lembrar
ROLES = [
    ("recruiter", "Recrutador / Talent", '(recruiter OR "talent acquisition" OR "talent partner")',
     "Recrutador(a) da empresa: costuma triar as candidaturas da vaga."),
    ("manager", "Hiring manager", '("design manager" OR "head of design" OR "design director" OR "product manager")',
     "Provável hiring manager: lidera design/produto e decide a contratação."),
    ("designer", "Designer do time", '("product designer" OR "ux designer")',
     "Designer do time: sabe como é o trabalho e pode indicar você por dentro."),
]
ROLE_LABEL = {k: label for k, label, _, _ in ROLES}
# Busca manual no LinkedIn (o candidato abre no navegador dele; nada é automatizado lá).
ROLE_KEYWORDS = {"recruiter": "recruiter OR talent", "manager": "design manager OR head of design OR product manager",
                 "designer": "product designer"}


def search_links(company):
    co = (company or "").split(",")[0].strip()
    return [{"role": k, "label": ROLE_LABEL[k],
             "url": "https://www.linkedin.com/search/results/people/?" + urllib.parse.urlencode(
                 {"keywords": f"{co} {ROLE_KEYWORDS[k]}", "origin": "GLOBAL_SEARCH_HEADER"})}
            for k in ROLE_KEYWORDS]
ROLE_WHY = {k: why for k, _, _, why in ROLES}
STATUS_UI = {"queued": "a conectar", "invited": "enviado por mim", "accepted": "aceitou",
             "messaged": "mensagem enviada", "replied": "respondeu", "skipped": "descartado"}
NOTE = {
    "recruiter": "Hi {first}, I saw {company} is hiring a {job}. I'm a Senior Product Designer (11+ yrs, AdTech, AI, fintech) based in Valencia, open to remote and contract roles. Would love to connect.",
    "manager": "Hi {first}, I saw your team at {company} is hiring a {job}. I'm a Senior Product Designer (11+ yrs, AdTech, AI, fintech), open to remote and contract roles. Would love to connect and follow your team's work.",
    "designer": "Hi {first}, I saw {company} is hiring a {job} and your work there caught my eye. I'm a Senior Product Designer (AdTech, AI, fintech) based in Valencia. Would love to connect.",
}


def _db():
    from agent import db
    return db()


def _now():
    from agent import now
    return now()


def init(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS job_wants (
      url TEXT PRIMARY KEY, created_at TEXT, job TEXT, searched_at TEXT, search_note TEXT);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(connections)")}
    if cols and "replied_at" not in cols:
        con.execute("ALTER TABLE connections ADD COLUMN replied_at TEXT")


def _ensure():
    with _db() as con:
        init(con)


def _rows(sql, args=()):
    with _db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def mark(job, on=True):
    """Marca/desmarca "Quero aplicar". `job` = a vaga como o painel recebe de /api/jobs."""
    if not job.get("url"):
        raise ValueError("vaga sem link")
    _ensure()
    keep = {k: job.get(k) for k in ("id", "title", "company", "url", "location", "country", "score", "contract")}
    with _db() as con:
        if on:
            con.execute("INSERT OR IGNORE INTO job_wants (url, created_at, job) VALUES (?,?,?)",
                        (job["url"], _now().isoformat(), json.dumps(keep, ensure_ascii=False)))
        else:
            con.execute("DELETE FROM job_wants WHERE url=?", (job["url"],))


def wanted_urls():
    _ensure()
    return {r["url"] for r in _rows("SELECT url FROM job_wants")}


def _first(name):
    return (name or "").split(" ")[0] or "there"


def note(person):
    kind = person.get("kind") or "recruiter"
    job = person.get("job_title") or "Product Designer"
    for j in (job, "Product Designer", "designer"):
        text = NOTE.get(kind, NOTE["recruiter"]).format(first=_first(person.get("person")),
                                                       company=person.get("company") or "", job=j)
        if len(text) <= 300:
            return text
    return text[:300]


def _kind(row):
    r = (row.get("role") or "")
    for k, label, _, _ in ROLES:
        if r == label:
            return k
    return "recruiter" if r.startswith("Recrut") else "manager"


def _search(company, title, role_q):
    """Uma consulta no DuckDuckGo HTML. Devolve perfis (link do resultado, nunca construído)."""
    from connect import PROFILE_RE
    words = " ".join((title or "").split()[:4])
    query = f'site:linkedin.com/in "{company}" {role_q}'
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", "replace")
    if "challenge-form" in page:
        raise RuntimeError("DuckDuckGo pediu verificação; tente mais tarde")
    token = company.split(",")[0].strip().lower()
    out = []
    for href, label in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', page, re.I | re.S):
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [""])[0])
        href = href.split("?")[0].strip()
        if not PROFILE_RE.match(href):
            continue
        label = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", label))).strip()
        if token not in label.lower():
            continue
        name = re.split(r"\s+[-|–]\s+", label)[0].strip()
        if name and name.lower() != token and all(o["url"] != href for o in out):
            out.append({"name": name, "url": href, "title": label, "query": query, "words": words})
    return out


def find(url, search=None):
    """Botão "Buscar pessoas" de uma vaga marcada: até 5 perfis novos, por papel."""
    from agent import log
    search = search or _search
    _ensure()
    w = _rows("SELECT * FROM job_wants WHERE url=?", (url,))
    if not w:
        raise ValueError("Marque a vaga como Quero aplicar primeiro.")
    job = json.loads(w[0]["job"])
    have = _rows("SELECT profile_url FROM connections WHERE job_url=? AND why='quero-aplicar'", (url,))
    known = {(h["profile_url"] or "").rstrip("/").lower() for h in have}
    added, errors = 0, []
    for k, label, role_q, _ in ROLES:
        if len(known) >= MAX_PEOPLE:
            break
        try:
            found = search(job.get("company") or "", job.get("title") or "", role_q)
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue
        for p in found[:2]:
            key = p["url"].rstrip("/").lower()
            if key in known or len(known) >= MAX_PEOPLE:
                continue
            known.add(key)
            _insert(job, url, label, p["name"], p["url"], p["title"], p.get("query", ""))
            added += 1
        time.sleep(1.5)
    note_txt = f"{added} pessoa(s) nova(s)" + (f" · falhou: {'; '.join(errors)}" if errors else "")
    with _db() as con:
        con.execute("UPDATE job_wants SET searched_at=?, search_note=? WHERE url=?", (_now().isoformat(), note_txt, url))
    log(f"want.find: {job.get('company')!r} → {note_txt}")
    return {"added": added, "errors": errors}


def _insert(job, url, role, name, profile, title, query):
    now = _now().isoformat()
    with _db() as con:
        con.execute(
            "INSERT INTO connections (created_at, company, role, why, search_url, job_title, job_url, contractor,"
            " score, lang, person, person_title, profile_url, found_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')",
            (now, job.get("company") or "", role, "quero-aplicar", query, job.get("title") or "", url,
             1 if job.get("contract") else 0, job.get("score") or 0, "en", name, title, profile, now))


def add_manual(url, profile, name, role_key):
    """Perfil colado por você (o link tem que ser de perfil do LinkedIn)."""
    from connect import PROFILE_RE
    profile = (profile or "").split("?")[0].strip()
    if not PROFILE_RE.match(profile):
        raise ValueError("Cole o link de um perfil (linkedin.com/in/…).")
    w = _rows("SELECT * FROM job_wants WHERE url=?", (url,))
    if not w:
        raise ValueError("Marque a vaga como Quero aplicar primeiro.")
    _insert(json.loads(w[0]["job"]), url, ROLE_LABEL.get(role_key, ROLE_LABEL["recruiter"]),
            (name or "").strip() or None, profile, "colado por você", "")


def act(cid, action):
    """Você mudou o status (clicou no painel). Nada é enviado daqui."""
    now = _now().isoformat()
    col = {"invited": "invited_at", "accepted": "accepted_at", "messaged": "messaged_at", "replied": "replied_at"}
    _ensure()
    with _db() as con:
        if action in col:
            con.execute(f"UPDATE connections SET status=?, {col[action]}=? WHERE id=?", (action, now, cid))
        elif action == "skip":
            con.execute("UPDATE connections SET status='skipped' WHERE id=?", (cid,))
        elif action == "queued":
            con.execute("UPDATE connections SET status='queued' WHERE id=?", (cid,))
        else:
            raise ValueError("ação desconhecida")


def _followup(c, now):
    st = c.get("status")
    at = {"invited": c.get("invited_at"), "accepted": c.get("accepted_at"), "messaged": c.get("messaged_at")}.get(st)
    if st not in FOLLOW_UP or not at:
        return ""
    try:
        days = (now - datetime.fromisoformat(at)).days
    except Exception:
        return ""
    if days < FOLLOW_UP[st]:
        return ""
    return {"invited": f"convite sem aceite há {days} dias: vale um lembrete ou outra pessoa",
            "accepted": f"aceitou há {days} dias: mande a mensagem",
            "messaged": f"mensagem sem resposta há {days} dias: follow-up curto"}[st]


def state():
    _ensure()
    from connect import message
    now = _now()
    wants = _rows("SELECT * FROM job_wants ORDER BY created_at DESC")
    people = _rows("SELECT * FROM connections WHERE why='quero-aplicar' AND status!='skipped' ORDER BY id")
    out = []
    for w in wants:
        job = json.loads(w["job"])
        ps = []
        for c in [p for p in people if p["job_url"] == w["url"]]:
            k = _kind(c)
            c = {**c, "kind": k}
            ps.append({"id": c["id"], "person": c.get("person") or "", "title": c.get("person_title") or "",
                       "profile_url": c.get("profile_url"), "role": c.get("role"), "kind": k,
                       "why": ROLE_WHY.get(k, ""), "status": c.get("status"), "status_ui": STATUS_UI.get(c.get("status"), c.get("status")),
                       "note": note(c), "message": message({**c, "lang": "en"}),
                       "followup": _followup(c, now)})
        out.append({"url": w["url"], "job": job, "created_at": w["created_at"], "searched_at": w["searched_at"],
                    "search_note": w["search_note"], "people": ps,
                    "search_links": search_links(job.get("company"))})
    return {"wants": out, "statuses": STATUS_UI}


def daily_text():
    """Lista simples do dia: vagas marcadas e o próximo passo de cada pessoa."""
    s = state()
    lines = [f"Quero aplicar · {_now().strftime('%d/%m/%Y')}", ""]
    for w in s["wants"]:
        j = w["job"]
        lines.append(f"■ {j.get('company')} · {j.get('title')}  {w['url']}")
        if not w["people"]:
            lines.append("   - ninguém ainda: clique em Buscar pessoas ou cole um perfil")
        for p in w["people"]:
            nxt = p["followup"] or {"queued": "conectar (nota pronta no painel)", "invited": "esperar o aceite",
                                    "accepted": "mandar a mensagem", "messaged": "esperar a resposta",
                                    "replied": "responder / marcar conversa"}.get(p["status"], "")
            lines.append(f"   - {p['person'] or '(sem nome)'} · {p['role']} · {p['status_ui']} → {nxt}  {p['profile_url']}")
        lines.append("")
    if not s["wants"]:
        lines.append("Nenhuma vaga marcada. No painel, aperte S numa vaga.")
    return "\n".join(lines).rstrip() + "\n"
