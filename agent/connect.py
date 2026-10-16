#!/usr/bin/env python3
"""Fila diária de conexões com quem está contratando.

O LinkedIn não dá API de convite nem de mensagem, e automação pelo navegador
arrisca restrição da conta. Então o agente escolhe os alvos e escreve as
mensagens; o clique em Conectar e o envio ficam com você.

Limites ficam abaixo do que costuma disparar alerta (~100 convites/semana).
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta

import criteria

PT_SLOT = "Brasileiros na empresa"
# IC and recruiters only. "Design Manager / Head of Design" was removed on
# 2026-10-14: that track is outside the Senior IC profile.
ROLE_ORDER = ["Recrutador / Talent", "Senior Product Designer (IC)", "Product Designers do time", PT_SLOT]

MSG = {
    "en": ("Hi {first}, thanks for connecting! I'm a Senior Product Designer (11+ years, AdTech, AI "
           "and fintech products) based in Valencia, open to remote full-time or contract roles. "
           "{job_line}Happy to share more if it's ever a fit. Portfolio: {portfolio}"),
    "pt": ("Oi {first}, obrigado por aceitar a conexão! Sou Senior Product Designer (11+ anos, AdTech, "
           "AI e fintech), moro em Valência e estou aberto a posições remotas, full-time ou contrato. "
           "{job_line}Se fizer sentido, adoraria conversar. Portfólio: {portfolio}"),
    "es": ("Hola {first}, ¡gracias por conectar! Soy Senior Product Designer (11+ años en AdTech, IA "
           "y fintech), vivo en Valencia y estoy abierto a roles remotos, full-time o por contrato. "
           "{job_line}Si en algún momento encaja, encantado de hablar. Portfolio: {portfolio}"),
}
JOB_LINE = {
    "en": "I saw {company} is hiring for {job} and it's right in my lane. ",
    "pt": "Vi que a {company} está contratando para {job}, bem na minha área. ",
    "es": "Vi que {company} está buscando {job} y es justo mi terreno. ",
}
NOTE = {
    "en": "Hi {first}, I saw {company} is hiring for {job}. I'm a Senior Product Designer (AdTech, AI, fintech) based in Valencia, open to remote and contract roles. {close}",
    "pt": "Oi {first}, vi que a {company} está contratando para {job}. Sou Senior Product Designer (AdTech, AI, fintech), em Valência, aberto a remoto e contrato. {close}",
    "es": "Hola {first}, vi que {company} busca {job}. Soy Senior Product Designer (AdTech, IA, fintech) en Valencia, abierto a remoto y contrato. {close}",
}
CLOSE = {
    "recruiter": {"en": "Would love to connect.", "pt": "Adoraria me conectar.", "es": "Me encantaría conectar."},
    "manager": {"en": "Would love to connect and follow your team's work.",
                "pt": "Adoraria me conectar e acompanhar o trabalho do time.",
                "es": "Me encantaría conectar y seguir el trabajo del equipo."},
    "peer": {"en": "Would love to connect, fellow Brazilian here too.",
             "pt": "Brasileiro também, adoraria me conectar!", "es": "Me encantaría conectar."},
}


def note(c):
    """Nota de convite (LinkedIn aceita até 300 caracteres)."""
    lang = c.get("lang") if c.get("lang") in NOTE else "en"
    kind = "peer" if c.get("role") == PT_SLOT else "recruiter" if (c.get("role") or "").startswith("Recrutador") else "manager"
    first = (c.get("person") or "").split(" ")[0] or "there"
    job = c.get("job_title") or ("product designers" if lang == "en" else "product designer")
    text = NOTE[lang].format(first=first, company=c["company"], job=job, close=CLOSE[kind][lang])
    if len(text) > 300:
        text = NOTE[lang].format(first=first, company=c["company"], job="product design", close=CLOSE[kind][lang])
    return text[:300]


GREETING_ONLY = {"en": "there", "pt": "", "es": ""}


def lang_for(role, location):
    if role == PT_SLOT:
        return "pt"
    loc = (location or "").lower()
    if "spain" in loc or "españa" in loc or "latam" in loc or "mexico" in loc or "argentina" in loc:
        return "es"
    return "en"


def init(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS connections (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT, company TEXT, role TEXT, why TEXT, search_url TEXT,
      job_title TEXT, job_url TEXT, contractor INTEGER DEFAULT 0, score INTEGER,
      lang TEXT DEFAULT 'en', person TEXT,
      status TEXT DEFAULT 'queued',
      invited_at TEXT, accepted_at TEXT, messaged_at TEXT);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(connections)")}
    for col in ("profile_url", "person_title", "found_at"):
        if col not in cols:
            con.execute(f"ALTER TABLE connections ADD COLUMN {col} TEXT")


def _db():
    from agent import db
    return db()


def _cfg(key, default=None):
    from agent import cfg
    return cfg().get(key, default)


def _now():
    from agent import now
    return now()


def sent_since(days):
    cut = (_now() - timedelta(days=days)).isoformat()
    with _db() as con:
        return con.execute("SELECT count(*) FROM connections WHERE invited_at >= ?", (cut,)).fetchone()[0]


def limits():
    per_day = _cfg("connect_per_day", 20)
    week_cap = _cfg("connect_week_cap", 100)
    today = sent_since(1)
    week = sent_since(7)
    return {"per_day": per_day, "week_cap": week_cap, "today": today, "week": week,
            "left_today": max(0, min(per_day - today, week_cap - week))}


def applied_companies():
    """Empresas em que você já se candidatou (status applied em diante)."""
    import applications
    _, by_pair = applications.applied_index()
    return {p[0] for p in by_pair}


def _priority(job, applied):
    """0 = você já aplicou; 1 = vaga quente (score 80+ e entrou nas últimas 72h); 2 = resto."""
    import applications
    if applications._norm(job.get("company")) in applied:
        return 0
    added = job.get("addedAt") or job.get("postedAt") or 0
    recent = added >= (_now() - timedelta(hours=72)).timestamp() * 1000
    if (job.get("score") or 0) >= 80 and recent:
        return 1
    return 2


WARM = ("interview scheduled", "offer", "applied", "hunting")
WARM_RANK = {"interview scheduled": 0, "offer": 0, "applied": 1, "hunting": 2}


def _profile(url):
    url = (url or "").split("?")[0].strip()
    return url if PROFILE_RE.match(url) else ""


_GENERIC_ROLE = {"linkedin", "email", "website", "contato da candidatura"}


def _person_label(url, role):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    parts = [part.capitalize() for part in slug.split("-") if part.isalpha()]
    role_clean = (role or "").strip()
    if role_clean.lower() in _GENERIC_ROLE:
        role_clean = ""
    if len(parts) >= 2:
        return " ".join(parts)
    return role_clean or (parts[0] if parts else slug)


def _sheet_contacts():
    """Perfil na planilha significa que o contato já foi feito. Não volta para a fila."""
    import applications
    found = set()
    for row in applications.list_all():
        url = _profile(row.get("contact"))
        if url:
            found.add(url.rstrip("/").lower())
    return found


def build_queue(top_up=False):
    """Tira da fila o radar sem perfil e quem já está como contato da candidatura.

    Não cria convite novo e não busca na web. top_up é ignorado.
    """
    del top_up
    today = _now().date().isoformat()
    contacts = _sheet_contacts()
    with _db() as con:
        con.execute(
            "UPDATE connections SET status='skipped' "
            "WHERE substr(created_at,1,10)=? AND status='queued' "
            "AND (profile_url IS NULL OR profile_url='')",
            (today,),
        )
        rows = con.execute(
            "SELECT id, profile_url FROM connections WHERE status='queued' AND profile_url IS NOT NULL"
        ).fetchall()
        done = [
            (row[0],)
            for row in rows
            if (row[1] or "").split("?")[0].rstrip("/").lower() in contacts
        ]
        if done:
            con.executemany("UPDATE connections SET status='skipped' WHERE id=?", done)
    return 0


PROFILE_RE = re.compile(r"^https://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?$")
FINDING = {"on": False}


def _wanted(r):
    if r["role"] == PT_SLOT:
        return "a Brazilian employee (ideally in design, product or recruiting)"
    if r["role"].startswith("Recrutador"):
        return "a technical recruiter / talent acquisition person"
    return "a senior product designer (individual contributor, not a head, lead, or design manager)"


def _find_one(r):
    """Uma chamada Claude (web search) para achar 1 pessoa num slot. Levanta em falha de CLI."""
    from agent import ask_json, log
    job = f' They are hiring for "{r["job_title"]}".' if r["job_title"] else ""
    prompt = f"""Find {_wanted(r)} currently working at the company "{r["company"]}".{job}
Use WebSearch, up to 4 searches, e.g.: site:linkedin.com/in "{r["company"]}" "talent acquisition".
Do not fetch linkedin.com pages; read the search results.
Only answer with a person if a result clearly shows their linkedin.com/in/ URL together with this company.
Never guess or construct a URL.
Also report where the person is based, if the result shows it.
Output ONLY JSON: {{"name": "...", "title": "...", "url": "https://www.linkedin.com/in/...", "location": "city, country or empty"}} or {{"name": null}}"""
    try:
        p = ask_json(prompt, allow_web=True)
    except Exception as e:
        log("find_people falhou:", r["company"], e)
        raise
    url = (p.get("url") or "").split("?")[0].strip()
    ok = bool(p.get("name") and PROFILE_RE.match(url))
    lang = "pt" if r["role"] == PT_SLOT else person_lang(p.get("location"), url)
    with _db() as con:
        con.execute(
            "UPDATE connections SET found_at=?, person=?, person_title=?, profile_url=?, lang=?, status=? WHERE id=?",
            (_now().isoformat(), p["name"] if ok else None, p.get("title") if ok else None,
             url if ok else None, lang, "queued" if ok else "skipped", r["id"]))
    return ok


def person_lang(location, url=""):
    """Idioma pela pessoa, não pela vaga: Brasil → PT, Espanha/LatAm hispânica → ES, resto → EN."""
    loc = (location or "").lower()
    if "brazil" in loc or "brasil" in loc or url.startswith("https://br.linkedin.com"):
        return "pt"
    if any(k in loc for k in ("spain", "españa", "mexico", "méxico", "argentina", "colombia", "chile", "peru")) \
            or url.startswith("https://es.linkedin.com"):
        return "es"
    return "en"


def find_people(limit=0, retry=False):
    """Manual only. Uma empresa por run, até 5 pessoas (Claude sequencial)."""
    del retry
    # limit arg ignored for companies — always 1 company; cap people inside suggest
    return suggest(limit=1)["found"]


def _role_for(blob):
    """Papel aceito no contato da vaga: recrutador, Design Manager, Head ou Product Manager."""
    if any(w in blob for w in ("recruiter", "talent acquisition", "talent partner", "sourcer", "people partner")):
        return "Recrutador / Talent"
    if "product manager" in blob or "head of product" in blob:
        return "Product Manager"
    if any(w in blob for w in ("design manager", "head of design", "design director")):
        return "Design Manager"
    if "head" in blob and ("design" in blob or "product" in blob):
        return "Head"
    return ""


def _targets(limit):
    """Só empresas de applied_companies(), sem contato ainda. Mais recente primeiro.
    Sem repetir tentativa de 7 dias. `limit` = nº de empresas (suggest usa 1)."""
    import applications
    applied = applied_companies()  # nomes normalizados (status applied em diante)
    cut = (_now() - timedelta(days=7)).isoformat()
    tried = {
        applications._norm(r["company"])
        for r in _rows(
            "SELECT company FROM connections WHERE why='sugerida' AND created_at>=?",
            (cut,),
        )
    }
    out, seen = [], set()
    # list_all já vem ORDER BY id DESC → candidatura mais recente primeiro
    for row in applications.list_all():
        company = (row.get("company") or "").strip()
        key = applications._norm(company)
        if not company or key not in applied:
            continue
        if _profile(row.get("contact")):
            continue
        if key in tried or key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _directed_query(company, job):
    title = " ".join((job or "").split()[:6])
    return (f'site:linkedin.com/in "{company}" "{title}" '
            '(recruiter OR "design manager" OR "head of design" OR "product manager")')


def _search_recruiter(company, job):
    """Uma consulta. Aceita recrutador, Design Manager, Head de design/produto ou Product Manager."""
    import html as htmlmod
    import urllib.parse
    import urllib.request
    query = _directed_query(company, job)
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", "replace")
    if "challenge-form" in page or "linkedin.com/in/" not in page.lower():
        return {"name": None, "query": query, "failed": True}
    token = company.split(",")[0].strip().lower()
    for href, label in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', page, re.I | re.S):
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [""])[0])
        href = href.split("?")[0].strip()
        if not PROFILE_RE.match(href):
            continue
        label = htmlmod.unescape(re.sub(r"<[^>]+>", " ", label))
        label = re.sub(r"\s+", " ", label).strip()
        blob = label.lower()
        if token not in blob:
            continue
        role = _role_for(blob)
        if not role:
            continue
        name = re.split(r"\s+[\-|–|]\s+", label)[0].strip()
        if not name or name.lower() == token:
            continue
        return {"name": name, "url": href, "title": label, "query": query, "role": role}
    return {"name": None, "query": query}


def _remember_attempt(row, found):
    now = _now().isoformat()
    url = (found or {}).get("url") or ""
    ok = bool(found and found.get("name") and PROFILE_RE.match(url))
    with _db() as con:
        con.execute(
            "INSERT INTO connections (created_at, company, role, why, search_url, job_title, job_url,"
            " contractor, score, lang, person, person_title, profile_url, found_at, status)"
            " VALUES (?,?,?,?,?,?,?,0,0,?,?,?,?,?,?)",
            (now, row["company"].strip(), (found or {}).get("role") or "Recrutador / Talent", "sugerida",
             (found or {}).get("query") or "", row.get("job_title") or "", row.get("job_url") or "",
             person_lang("", url), found.get("name") if ok else None, found.get("title") if ok else None,
             url if ok else None, now, "queued" if ok else "skipped"),
        )
    return ok


def _queue_stub(row, role):
    """Cria slot vazio na fila pra _find_one preencher (ou marcar skipped)."""
    now = _now().isoformat()
    with _db() as con:
        cur = con.execute(
            "INSERT INTO connections (created_at, company, role, why, search_url, job_title, job_url,"
            " contractor, score, lang, status) VALUES (?,?,?,?,?,?,?,0,0,'en','queued')",
            (now, row["company"].strip(), role, "sugerida", "",
             row.get("job_title") or "", row.get("job_url") or ""),
        )
        return cur.lastrowid


def suggest(limit=1):
    """Manual only. 1 empresa/run (candidatura mais recente), até 5 pessoas, Claude sequencial.
    Não retenta empresa que falhou no mesmo run. Nunca chamada por daily/page-load."""
    from agent import log
    # Sempre 1 empresa — ignore limit>1 do caller antigo
    max_people = 5
    if FINDING["on"]:
        return {"ok": True, "tried": 0, "found": 0, "count": 0}
    FINDING["on"] = True
    tried = found = 0
    try:
        targets = _targets(1)
        if not targets:
            log("suggest: nenhuma empresa applied sem contato")
            return {"ok": True, "tried": 0, "found": 0, "count": 0}
        row = targets[0]
        tried = 1
        log(f"suggest: empresa={row.get('company')!r} (1 empresa, até {max_people} pessoas)")
        for role in ROLE_ORDER[:max_people]:
            if found >= max_people:
                break
            rid = _queue_stub(row, role)
            slot = {
                "id": rid,
                "company": row["company"].strip(),
                "role": role,
                "job_title": row.get("job_title") or "",
            }
            try:
                ok = _find_one(slot)
            except Exception:
                # Falha Claude nesta empresa → não tenta de novo no mesmo run
                with _db() as con:
                    con.execute("UPDATE connections SET status='skipped' WHERE id=?", (rid,))
                log("suggest: empresa falhou, abortando run:", row.get("company"))
                break
            if ok:
                found += 1
        return {"ok": True, "tried": tried, "found": found, "count": found}
    finally:
        FINDING["on"] = False


def message(c, first=""):
    lang = c.get("lang") if c.get("lang") in MSG else "en"
    job_line = JOB_LINE[lang].format(company=c["company"], job=c["job_title"]) if c.get("job_title") else ""
    first = (first or c.get("person") or "").split(" ")[0] or GREETING_ONLY[lang]
    text = MSG[lang].format(first=first, job_line=job_line, portfolio=_cfg("portfolio_url", ""))
    return text.replace("Oi ,", "Oi,").replace("Hola ,", "Hola,")


def _rows(sql, args=()):
    with _db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def _stamp_sheet(cid):
    """Convite marcado: aí o perfil entra na coluna de contato da candidatura."""
    import applications
    row = _rows("SELECT * FROM connections WHERE id=?", (cid,))
    if not row or not row[0].get("profile_url"):
        return
    row = row[0]
    for app in applications.list_all():
        if applications._norm(app.get("company")) != applications._norm(row.get("company")):
            continue
        if _profile(app.get("contact")):
            continue
        applications.update(app["id"], {
            "contact": row["profile_url"],
            "contact_role": row.get("person_title") or row.get("role") or "",
            "contact_method": "Linkedin",
        })
        return


def state():
    contacts = _sheet_contacts()
    allq = _rows(
        "SELECT * FROM connections WHERE status='queued' AND why='sugerida' AND profile_url IS NOT NULL"
        " ORDER BY id DESC"
    )
    allq = [c for c in allq if (c.get("profile_url") or "").split("?")[0].rstrip("/").lower() not in contacts]
    queue = allq[:max(0, limits()["left_today"])]
    for c in queue:
        c["note"] = note(c)
    searching = 0
    invited = _rows("SELECT * FROM connections WHERE status='invited' ORDER BY invited_at DESC LIMIT 60")
    accepted = _rows("SELECT * FROM connections WHERE status='accepted' ORDER BY accepted_at DESC")
    for c in invited + accepted:
        c["message"] = message(c)
    with _db() as con:
        funnel = {k: con.execute(f"SELECT count(*) FROM connections WHERE {col} IS NOT NULL").fetchone()[0]
                  for k, col in (("invited", "invited_at"), ("accepted", "accepted_at"), ("messaged", "messaged_at"))}
    return {"queue": queue, "invited": invited, "accepted": accepted, "limits": limits(), "funnel": funnel,
            "finding": FINDING["on"], "searching": searching}


# Regras duras (revisão 2026-10-16): as mesmas da coleta e do pool, de monitor/job-criteria.json,
# aplicadas pelo criteria.py (espelho do filters.js; paridade em monitor/test-criteria.js).
# Vaga que alguma regra exclui some do radar; vaga sem texto aparece marcada como pendente, pra abrir na mão.
_ASSESSED = {}
_ASSESS_FIELDS = ("title", "company", "location", "description", "salary", "tags", "source", "seniority",
                  "employmentType", "closed", "descriptionFinal")


def _assess(j):
    """criteria.assess com memória: custa uns 4 ms por vaga e o painel pede a lista a cada visita.
    A chave muda quando a vaga ou o job-criteria.json mudam."""
    st = os.stat(criteria.RULES_FILE)
    key = (st.st_mtime_ns, st.st_size, j.get("id") or j.get("url"),
           hash(tuple(str(j.get(k)) for k in _ASSESS_FIELDS)))
    a = _ASSESSED.get(key)
    if a is None:
        if len(_ASSESSED) > 5000:
            _ASSESSED.clear()
        a = _ASSESSED[key] = criteria.assess(j)
    return a


def jobs(hidden=None):
    """Radar de vagas (o mesmo do Telegram), maior score primeiro, com o status de contato na empresa.
    Vaga que uma regra dura exclui não entra; se `hidden` for um dict, ele recebe a contagem por motivo.
    Vaga sem texto entra com `pending` (o motivo) pra você abrir e decidir."""
    from engage import load_jobs
    with _db() as con:
        touched = {(r[0] or "").lower(): r[1] for r in con.execute(
            "SELECT company, max(CASE status WHEN 'accepted' THEN 3 WHEN 'messaged' THEN 4 "
            "WHEN 'invited' THEN 2 WHEN 'queued' THEN 1 ELSE 0 END) FROM connections GROUP BY company")}
    label = {1: "na fila de hoje", 2: "convidado", 3: "aceitou", 4: "mensagem enviada"}
    cut = (_now() - timedelta(days=1)).timestamp() * 1000
    import applications
    by_url, by_pair = applications.applied_index()
    import jobfeedback
    pens = jobfeedback._read_json(jobfeedback.PENALTIES, {})
    out = []
    for j in load_jobs():
        a = _assess(j)
        x = a["exclude"]
        if x and x["key"] != "needs_description":
            if hidden is not None:
                hidden[x["label"]] = hidden.get(x["label"], 0) + 1
            continue
        added = j.get("addedAt") or j.get("postedAt") or 0
        out.append({k: j.get(k) for k in ("id", "title", "company", "url", "location", "source", "score",
                                          "scoreReasons", "fit", "ats", "seniority", "employmentType")}
                   | {"added": added, "new": added >= cut,
                      "contractor": bool(a["contractor"]["type"]), "contract": a["contractor"]["type"] or "",
                      "labels": a["labels"], "country": a["country"] or "", "lang": a["lang"] or "",
                      "currency": {"status": a["currency"]["status"], "codes": a["currency"]["codes"]},
                      "remote": {"how": a["remote"]["how"], "evidence": a["remote"]["evidence"]},
                      "pending": x["evidence"] if x else "",
                      "applied": applications._job_id(j.get("url")) in by_url and bool(applications._job_id(j.get("url")))
                                 or (applications._norm(j.get("company")), applications._norm(j.get("title"))) in by_pair,
                      "company_applied": any(applications._norm(j.get("company")) == p[0] for p in by_pair),
                      "contact": label.get(touched.get((j.get("company") or "").lower()), "")})
        pts, why = jobfeedback.penalty({**j, "country": a["country"]}, pens)
        if pts and not j.get("learnedPenalty"):  # o rank.js já aplica na coleta; aqui só pra vaga antiga
            out[-1]["score"] = (j.get("score") or 0) + pts
        out[-1]["penalty"] = why
    return sorted(out, key=lambda j: (-(j["score"] or 0), -j["added"]))


def act(cid, action, body):
    now = _now().isoformat()
    with _db() as con:
        if action == "invited":
            if limits()["left_today"] <= 0:
                raise ValueError("Limite do dia/semana atingido. Pare por hoje para proteger a conta.")
            con.execute("UPDATE connections SET status='invited', invited_at=?,"
                        " person=COALESCE(NULLIF(?, ''), person), lang=? WHERE id=?",
                        (now, (body.get("person") or "").strip(), body.get("lang") or "en", cid))
        elif action == "accepted":
            con.execute("UPDATE connections SET status='accepted', accepted_at=? WHERE id=?", (now, cid))
        elif action == "messaged":
            con.execute("UPDATE connections SET status='messaged', messaged_at=? WHERE id=?", (now, cid))
        elif action == "skip":
            con.execute("UPDATE connections SET status='skipped' WHERE id=?", (cid,))
        elif action == "lang":
            con.execute("UPDATE connections SET lang=? WHERE id=?", (body.get("lang") or "en", cid))
        else:
            raise ValueError("ação desconhecida")
    if action == "invited":
        _stamp_sheet(cid)


def daily():
    """Scheduler: só limpa fila. NUNCA chama find_people/suggest/Claude (só botão manual)."""
    from agent import notify, log
    try:
        n = build_queue()
        # auto_find_people fica off por default; mesmo se True, preferimos o botão
        # explícito — não disparamos Claude aqui pra proteger a cota.
        if _cfg("auto_find_people"):
            log("connect.daily: auto_find_people=true ignorado — busque pelo botão do painel")
        acc = len(_rows("SELECT id FROM connections WHERE status='accepted'"))
        notify(f"{n} pessoas para conectar hoje" + (f" · {acc} aceitaram, mande a mensagem" if acc else ""),
               "Conexões")
    except Exception as e:
        log("connect.daily falhou:", e)


# Revisão 2026-10-16 (Parte 2/3): status da coleta e texto da vaga sob demanda pro painel.
def collect_status():
    """Lê monitor/data/collect-status.json (gravado pelo job-monitor a cada rodada)."""
    from engage import HERMES_ENGAGE_JSON
    f = HERMES_ENGAGE_JSON.parent / "collect-status.json"
    if not f.exists():
        return {"missing": True}
    try:
        s = json.loads(f.read_text())
    except Exception as e:  # arquivo sendo gravado
        return {"missing": True, "error": str(e)}
    now = _now().timestamp() * 1000
    last = s.get("lastOkAt") or 0
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            last = 0
    li = next((x for x in s.get("sources", []) if x.get("name") == "LinkedIn"), {})
    try:
        labels = {k: v.get("label", k) for k, v in json.loads(open(criteria.RULES_FILE).read())["rules"].items()}
    except Exception:
        labels = {}
    labels["applied"] = "já aplicou"
    return {**{k: s.get(k) for k in ("startedAt", "finishedAt", "ok", "error", "errorCount", "durationMs",
                                      "lastOkAt", "counts", "pool", "excluded")},
            "excludedLabels": labels,
            "ageMin": round((now - last) / 60000) if last else None,
            "stale": not last or now - last > 2 * 3600 * 1000,
            "linkedin": {k: li.get(k) for k in ("found", "requests", "http429", "details", "blocked", "skipped")},
            "sources": [{"name": x.get("name"), "found": x.get("found"), "errors": (x.get("errors") or [])[:5]}
                        for x in s.get("sources", [])],
            "history": (s.get("history") or [])[:24]}


def job_text(jid):
    from engage import load_jobs
    for j in load_jobs():
        if str(j.get("id")) == str(jid):
            a = _assess(j)
            return {"id": jid, "text": j.get("description") or "", "remote": a["remote"],
                    "contractor": a["contractor"], "currency": a["currency"], "hits": list(a["hits"].keys())
                    if isinstance(a["hits"], dict) else a["hits"]}
    return {"id": jid, "text": "", "missing": True}
