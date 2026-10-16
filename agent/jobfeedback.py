#!/usr/bin/env python3
"""Feedback de vagas: o usuário rejeita uma vaga e diz o porquê. Isso fica guardado
e vira padrões visíveis (motivos mais comuns, palavras do título, empresas) que
podem depois alimentar o ranking do job-monitor.

Nada aqui aplica filtro sozinho: a vaga some da aba Vagas, e os padrões aparecem
para o usuário confirmar antes de virarem regra."""
import json
import re
from collections import Counter
from datetime import datetime, timezone

# Motivos do popover (revisão 2026-10-16): as mesmas categorias da `ui` das regras do job-criteria.json.
REASONS = {
    "remote": "Não é remoto",
    "spain": "Espanha local",
    "language": "Idioma",
    "seniority": "Senioridade",
    "currency": "Moeda",
    "company": "Empresa/setor",
    "other": "Outro",
}
# Chaves antigas (antes de 2026-10-16) -> categoria nova. "location" vira Espanha se o local/nota falar de Espanha.
LEGACY = {"senior": "seniority", "junior": "seniority", "not_design": "company", "agency": "company",
          "location": "remote", "contract": "other", "salary": "currency"}
UI = {"remote": "Não é remoto", "spain": "Espanha local", "language": "Idioma", "seniority": "Senioridade",
      "currency": "Moeda", "company": "Empresa/setor", "other": "Outro"}

STOPWORDS = set("de da do das dos e em para com the of and a an for to in at on senior sênior "
                "lead head principal product designer design ux ui remote remoto remota contract contractor freelance staff mid level full time part hybrid europe emea worldwide anywhere".split())


def _db():
    from agent import db
    return db()


def init(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS job_rejections (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT, url TEXT, title TEXT, company TEXT, location TEXT,
      score INTEGER, fit TEXT, reasons TEXT, note TEXT);
    CREATE INDEX IF NOT EXISTS job_rejections_url ON job_rejections(url);
    """)


def _ensure():
    with _db() as con:
        init(con)


def rejected_urls():
    _ensure()
    with _db() as con:
        return {r[0] for r in con.execute("SELECT url FROM job_rejections")}


def reject(job, reasons, note=""):
    """Registra a rejeição. `reasons` = lista de chaves de REASONS. `note` = texto livre."""
    reasons = [LEGACY.get(r, r) for r in (reasons or [])]
    reasons = list(dict.fromkeys(r for r in reasons if r in REASONS))
    note = (note or "").strip()
    if not job.get("url"):
        raise ValueError("vaga sem link")
    if not reasons and not note:
        raise ValueError("Diga pelo menos um motivo (ou escreva por quê).")
    _ensure()
    with _db() as con:
        con.execute("DELETE FROM job_rejections WHERE url=?", (job["url"],))
        con.execute(
            "INSERT INTO job_rejections (created_at, url, title, company, location, score, fit, reasons, note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), job["url"], job.get("title") or "",
             job.get("company") or "", job.get("location") or "", job.get("score"),
             json.dumps(job.get("fit") or [], ensure_ascii=False),
             json.dumps(reasons), note))
    _append_feedback(job, reasons, note)
    learn()


def unreject(url):
    _ensure()
    with _db() as con:
        con.execute("DELETE FROM job_rejections WHERE url=?", (url,))
    _append_feedback({"url": url}, [], "", undo=True)
    learn()


def _words(title):
    return [w for w in re.findall(r"[a-zà-ú]+", (title or "").lower())
            if len(w) > 2 and w not in STOPWORDS]


def patterns():
    """Resumo do que já foi aprendido, para mostrar na aba Vagas."""
    _ensure()
    with _db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM job_rejections ORDER BY id DESC")]
    reasons = Counter()
    words = Counter()
    companies = Counter()
    notes = []
    for r in rows:
        for k in _cats(r):
            reasons[k] += 1
        for w in set(_words(r["title"])):
            words[w] += 1
        companies[r["company"]] += 1
        if r["note"]:
            notes.append({"title": r["title"], "company": r["company"], "note": r["note"]})
    return {
        "total": len(rows),
        "reasons": [{"key": k, "label": REASONS.get(k, k), "count": n} for k, n in reasons.most_common()],
        "title_words": [{"word": w, "count": n} for w, n in words.most_common(10) if n >= 2],
        "companies": [{"company": c, "count": n} for c, n in companies.most_common(5) if n >= 2],
        "notes": notes[:15],
        "learned": _read_json(PENALTIES, {}),
        "suggestions": suggestions(),
        "approved": [l for l in _criteria().get("learned", [])],
    }


def reasons_catalog():
    return [{"key": k, "label": v} for k, v in REASONS.items()]



# ---------------------------------------------------------------------------------------------------------------
# Aprender com as rejeições (Parte 4, revisão 2026-10-16)
# - Cada rejeição vira uma linha em monitor/data/feedback.jsonl (com país, local e categoria).
# - learn() recalcula data/learned-penalties.json: empresa/país/palavra do título rejeitados 2+ vezes perdem pontos
#   no ranking (rank.js na coleta, connect.py no painel).
# - Motivo + valor que se repete 3+ vezes vira SUGESTÃO de regra dura. Nada é aplicado sozinho: só o botão
#   "Aprovar regra" no painel grava em job-criteria.json (learned), com backup antes.
import os
import shutil
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_DIR") or Path(__file__).resolve().parent.parent / "monitor")
FEEDBACK = HERMES / "data" / "feedback.jsonl"
PENALTIES = HERMES / "data" / "learned-penalties.json"
DISMISSED = HERMES / "data" / "learned-dismissed.json"
CRITERIA = HERMES / "job-criteria.json"
PENALTY_PER, PENALTY_MAX, PENALTY_MIN_COUNT, SUGGEST_AT = 10, 30, 2, 3


def _read_json(f, default):
    try:
        return json.loads(Path(f).read_text())
    except Exception:
        return default


def _criteria():
    return _read_json(CRITERIA, {})


def _cats(r):
    """Categorias de uma linha de job_rejections, traduzindo as chaves antigas."""
    out = []
    spain = re.search(r"spain|espa[nñ]a|madrid|barcelona|valencia", f"{r.get('location')} {r.get('note')}", re.I)
    for k in json.loads(r.get("reasons") or "[]"):
        k = "spain" if k == "location" and spain else LEGACY.get(k, k)
        if k in REASONS and k not in out:
            out.append(k)
    return out or ["other"]


def _country(job):
    if job.get("country"):
        return job["country"]
    try:
        import criteria
        return criteria.country_of(job.get("location") or "") or ""
    except Exception:
        return ""


def _append_feedback(job, reasons, note, undo=False, at=None):
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": at or datetime.now(timezone.utc).isoformat(), "url": job.get("url") or ""}
    if undo:
        row["undo"] = True
    else:
        row.update({"id": job.get("id") or "", "title": job.get("title") or "", "company": job.get("company") or "",
                    "location": job.get("location") or "", "country": _country(job), "source": job.get("source") or "",
                    "score": job.get("score"), "reasons": reasons, "categories": [UI[r] for r in reasons if r in UI],
                    "note": note})
    with open(FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def migrate():
    """Uma vez só: copia as rejeições antigas do banco pro feedback.jsonl (se ele ainda não existe)."""
    if FEEDBACK.exists():
        return 0
    _ensure()
    with _db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM job_rejections ORDER BY id")]
    for r in rows:
        _append_feedback(r, _cats(r), r.get("note") or "", at=r.get("created_at"))
    if not rows:
        FEEDBACK.touch()
    return len(rows)


def feedback_rows():
    """Rejeições valendo agora: as linhas do jsonl, menos as desfeitas depois."""
    rows = {}
    if FEEDBACK.exists():
        for line in FEEDBACK.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("undo"):
                rows.pop(r.get("url"), None)
            else:
                rows[r.get("url")] = r
    return list(rows.values())


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def learn(rows=None):
    """Recalcula as penalidades a partir do feedback e grava learned-penalties.json."""
    if rows is None:
        migrate()
        rows = feedback_rows()
    comp, ctry, word = Counter(), Counter(), Counter()
    for r in rows:
        if r.get("company"):
            comp[_norm(r["company"])] += 1
        if r.get("country"):
            ctry[_norm(r["country"])] += 1
        for w in set(_words(r.get("title"))):
            word[w] += 1
    pen = lambda n: -min(PENALTY_MAX, PENALTY_PER * n)
    out = {"updatedAt": datetime.now(timezone.utc).isoformat(), "rejections": len(rows),
           "rule": f"-{PENALTY_PER} por rejeição (até -{PENALTY_MAX}) a partir de {PENALTY_MIN_COUNT}",
           "company": {k: pen(n) for k, n in comp.items() if n >= PENALTY_MIN_COUNT},
           "country": {k: pen(n) for k, n in ctry.items() if n >= PENALTY_MIN_COUNT},
           "word": {k: pen(n) for k, n in word.items() if n >= PENALTY_MIN_COUNT}}
    if rows is not None and FEEDBACK.parent.exists():
        PENALTIES.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def penalty(job, pens=None):
    """(pontos, motivos) que as rejeições passadas tiram desta vaga. Mesma conta do rank.js."""
    p = pens if pens is not None else _read_json(PENALTIES, {})
    pts, why = 0, []
    c = p.get("company", {}).get(_norm(job.get("company")))
    if c:
        pts += c; why.append(f"empresa rejeitada antes ({c})")
    k = p.get("country", {}).get(_norm(job.get("country") or _country(job)))
    if k:
        pts += k; why.append(f"país rejeitado antes ({k})")
    ws = [p.get("word", {}).get(w) for w in set(_words(job.get("title")))]
    ws = [w for w in ws if w]
    if ws:
        w = max(-PENALTY_MAX, sum(ws)); pts += w; why.append(f"palavra do título rejeitada antes ({w})")
    return max(-60, pts), why


def _sid(kind, value, cat):
    return re.sub(r"[^a-z0-9]+", "-", f"{kind}-{value}-{cat}".lower()).strip("-")[:80]


def suggestions(rows=None):
    """Motivo + empresa/país/palavra repetidos 3+ vezes. Só sugere; aprovar é com o candidato."""
    rows = feedback_rows() if rows is None else rows
    done = {l.get("id") for l in _criteria().get("learned", [])} | set(_read_json(DISMISSED, []))
    cnt, ex = Counter(), {}
    for r in rows:
        cats = r.get("categories") or ["Outro"]
        keys = [("company", _norm(r.get("company")))] if r.get("company") else []
        if r.get("country"):
            keys.append(("country", _norm(r["country"])))
        keys += [("word", w) for w in set(_words(r.get("title")))]
        for kind, val in keys:
            for c in cats:
                cnt[(kind, val, c)] += 1
                ex.setdefault((kind, val, c), []).append(f"{r.get('title')} · {r.get('company')}")
    out = []
    for (kind, val, c), n in cnt.most_common():
        sid = _sid(kind, val, c)
        if n < SUGGEST_AT or sid in done:
            continue
        field = {"company": "company", "country": "location", "word": "title"}[kind]
        what = {"company": "empresa", "country": "país", "word": "palavra no título"}[kind]
        out.append({"id": sid, "kind": kind, "value": val, "category": c, "count": n,
                    "text": f"Excluir vagas com {what} “{val}” ({c}) — rejeitou {n} vezes",
                    "rule": {"id": sid, "field": field, "label": f"regra aprendida: {what} {val}", "ui": c,
                             "re": r"\b" + re.escape(val) + r"\b"},
                    "examples": ex[(kind, val, c)][:4]})
    return out[:10]


def approve(sid):
    """Clique do candidato: grava a regra sugerida em job-criteria.json (learned), com backup antes."""
    s = next((x for x in suggestions() if x["id"] == sid), None)
    if not s:
        raise ValueError("Sugestão não encontrada (talvez já aprovada).")
    raw = CRITERIA.read_text()
    data = json.loads(raw)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(CRITERIA, CRITERIA.with_name(f"job-criteria.json.bak-{stamp}-regra"))
    data.setdefault("learned", []).append({**s["rule"], "approvedAt": datetime.now(timezone.utc).isoformat(),
                                           "from": f"{s['count']} rejeições"})
    CRITERIA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return s["rule"]


def dismiss(sid):
    d = _read_json(DISMISSED, [])
    if sid not in d:
        d.append(sid)
        DISMISSED.write_text(json.dumps(d))
