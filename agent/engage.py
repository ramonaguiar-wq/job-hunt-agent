#!/usr/bin/env python3
"""Fila diária de engajamento: em quem comentar hoje, e o que escrever.

Por que existe: comentar bem no post de quem decide a contratação rende mais
visualização de perfil (e mais conversa) do que publicar mais um post. A fila
mistura duas origens:

  - 'job'   → senior product designer (IC) ou recrutador de empresas que estão
              com vaga aberta que passou no ranking do job-monitor. Head, Lead,
              Staff e Design Manager ficam fora. Todo comentário tem caminho de
              volta pra uma vaga concreta.
  - 'voice' → vozes grandes do nicho (prompts/voices.md), que dão alcance mesmo
              sem vaga aberta.

Limite conhecido: o LinkedIn não expõe leitura de posts nem de comentários por API
(a mesma parede do formato 'take'). Então o agente faz a seleção do alvo e escreve
o comentário, mas quem abre o post e cola o texto é você. Sem o texto colado ele
se recusa a inventar — comentário genérico é pior que nenhum.

Os módulos importam de agent.py dentro das funções (import tardio) porque o
agent.py importa este arquivo pras rotas web; no topo daria import circular.
"""
import json
import re
import urllib.parse
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
VOICES_FILE = PROMPTS / "voices.md"

# O job-monitor (hermes-orchestrator) escreve esse arquivo. Só leitura — este
# agente nunca fala com as APIs de vaga direto.
HERMES_ENGAGE_JSON = Path(__file__).resolve().parent.parent / "monitor" / "data" / "engage-jobs.json"

# Papéis que valem a pena alcançar numa empresa com vaga aberta, em ordem:
# quem decide primeiro, quem tria depois.
# As aspas são o que faz a busca parar de trazer "qualquer pessoa": sem elas o
# LinkedIn quebra os termos soltos e devolve qualquer designer que passou perto
# da palavra. Empresa entre aspas + cargo entre aspas restringe de verdade.
# Senior IC only. Head, Lead, Staff and Design Manager are out of the search
# profile (perfil/profile.md, 2026-10-14). Do not put them back as JOB_ROLES[0]:
# build_queue() always uses the first entry.
JOB_ROLES = [
    ("Senior Product Designer (IC)", "Par sênior no time — IC, não gestor",
     '"{company}" ("senior product designer" OR "product designer") -"head of" -"design manager" -"design lead"'),
    ("Recrutador / Talent", "Quem faz a triagem — reconhecer seu nome ajuda na pilha de CVs",
     '"{company}" ("technical recruiter" OR "talent acquisition" OR "talent partner")'),
]


# ---------------------------------------------------------------- schema
def init(con):
    """Cria as tabelas da fila. Chamado pelo init_db() do agent.py."""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS targets (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT, kind TEXT, person TEXT, role TEXT, company TEXT,
      why TEXT, angle TEXT, proof TEXT,
      job_title TEXT, job_url TEXT, search_url TEXT, activity_url TEXT,
      connect_note TEXT, score INTEGER,
      status TEXT DEFAULT 'queued',
      post_url TEXT, post_text TEXT, comment TEXT, options TEXT,
      engaged_at TEXT, followup_at TEXT, followup_done INTEGER DEFAULT 0);
    """)


def _db():
    from agent import db
    return db()


def _cfg(key, default=None):
    from agent import cfg
    return cfg().get(key, default)


def _now():
    from agent import now
    return now()


# ---------------------------------------------------------------- fontes
def load_jobs():
    """Vagas ranqueadas que o job-monitor deixou prontas."""
    try:
        data = json.loads(HERMES_ENGAGE_JSON.read_text())
    except Exception:
        return []
    return data.get("jobs", [])


def people_search(query):
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + urllib.parse.quote(query) + "&origin=GLOBAL_SEARCH_HEADER")


def content_search(query):
    """Posts recentes sobre/de alguém — é aqui que você acha o que comentar."""
    return ("https://www.linkedin.com/search/results/content/?keywords="
            + urllib.parse.quote(query) + "&sortBy=%22date_posted%22")


def parse_voices():
    """prompts/voices.md — uma voz por linha: `- Nome | por que | url ou busca`.

    Só linhas de lista no nível zero contam. O resto do arquivo é instrução, e os
    exemplos ficam em bloco indentado justamente pra não virarem alvo.
    """
    if not VOICES_FILE.exists():
        return []
    out = []
    for raw in VOICES_FILE.read_text().splitlines():
        if not raw[:1] in ("-", "*"):   # indentado ou prosa = não é alvo
            continue
        line = raw.strip()
        parts = [p.strip() for p in line.lstrip("-* ").split("|")]
        if len(parts) < 2:
            continue
        name, why = parts[0], parts[1]
        ref = parts[2] if len(parts) > 2 else ""
        out.append({
            "person": name,
            "why": why,
            # Tema entre aspas também aqui: solto, a busca devolve qualquer post
            # que contenha uma das palavras.
            "activity_url": ref if ref.startswith("http") else content_search('"%s"' % (ref or name)),
        })
    return out


# ---------------------------------------------------------------- construção da fila
def _recent_keys(days=30):
    """Quem já entrou na fila há pouco tempo — pra não repetir alvo."""
    cut = (_now() - timedelta(days=days)).isoformat()
    with _db() as con:
        rows = con.execute(
            "SELECT company, person FROM targets WHERE created_at >= ?", (cut,)).fetchall()
    return {(r["company"] or "").lower() for r in rows}, {(r["person"] or "").lower() for r in rows}


def build_queue(n_jobs=None, n_voices=None):
    """Monta a fila do dia. Idempotente: não duplica alvo do mesmo dia."""
    n_jobs = n_jobs if n_jobs is not None else _cfg("engage_jobs_per_day", 5)
    n_voices = n_voices if n_voices is not None else _cfg("engage_voices_per_day", 2)
    today = _now().date().isoformat()

    with _db() as con:
        already = con.execute(
            "SELECT count(*) FROM targets WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0]
    if already:
        return already

    seen_companies, seen_people = _recent_keys()
    rows = []

    for job in load_jobs()[: n_jobs * 3]:
        company = (job.get("company") or "").strip()
        if not company or company.lower() in seen_companies:
            continue
        seen_companies.add(company.lower())

        people = job.get("people") or {}
        notes = people.get("notes") or {}
        named = (people.get("named") or [])
        # Se a própria vaga citou o gestor pelo nome, esse é o alvo; senão, busca por cargo.
        if named:
            person, role, why = named[0]["name"], "Citado na vaga", named[0]["why"]
            search = people_search('"%s" "%s"' % (named[0]["name"], company))
        else:
            role, why, tmpl = JOB_ROLES[0]
            person, search = "", people_search(tmpl.format(company=company))
        # Nome de empresa curto/genérico ("Fin") não funciona como busca de conteúdo:
        # qualifica com o domínio pra trazer posts do time, não a palavra solta.
        feed_query = '"%s" design' % company if len(company) <= 6 else '"%s"' % company

        rows.append({
            "kind": "job", "person": person, "role": role, "company": company,
            "why": why,
            "angle": _angle_from_fit(job.get("fit") or []),
            "proof": _proof_from_fit(job.get("fit") or []),
            "job_title": job.get("title"), "job_url": job.get("url"),
            "search_url": search,
            "activity_url": content_search(feed_query),
            "connect_note": notes.get("designer") or notes.get("recruiter") or "",
            "score": job.get("score") or 0,
        })
        if len(rows) >= n_jobs:
            break

    for v in parse_voices():
        if v["person"].lower() in seen_people:
            continue
        if len([r for r in rows if r["kind"] == "voice"]) >= n_voices:
            break
        rows.append({
            "kind": "voice", "person": v["person"], "role": "Voz do nicho",
            "company": "", "why": v["why"], "angle": "", "proof": "",
            "job_title": None, "job_url": None,
            "search_url": people_search(v["person"]),
            "activity_url": v["activity_url"], "connect_note": "", "score": 0,
        })

    with _db() as con:
        for r in rows:
            con.execute(
                "INSERT INTO targets (created_at, kind, person, role, company, why, angle, proof,"
                " job_title, job_url, search_url, activity_url, connect_note, score)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now().isoformat(), r["kind"], r["person"], r["role"], r["company"], r["why"],
                 r["angle"], r["proof"], r["job_title"], r["job_url"], r["search_url"],
                 r["activity_url"], r["connect_note"], r["score"]))
    return len(rows)


# Métricas reais do perfil, escolhidas pelo tema da vaga. Mesma tabela do people.js
# do hermes — o comentário precisa carregar prova concreta, não adjetivo.
PROOF = {
    "Retail Media / AdTech": "redesign do leilão do iFood Ads: +33,4% GMV, +23,1% CTR",
    "AI / LLM": "produto de insights com LLM no iFood Ads",
    "Fintech / pagamentos": "KYC e onboarding unificado na Caju",
    "Design System": "design system da Caju (iF Design Award 2026)",
    "B2B SaaS": "redesign do leilão do iFood Ads: +33,4% GMV",
}
ANGLE = {
    "AI / LLM": "como AI muda a superfície do produto, não só o backend",
    "Retail Media / AdTech": "trade-offs de leilão e monetização vistos pelo lado do design",
    "Fintech / pagamentos": "fricção de onboarding/KYC sem perder conversão",
    "Design System": "governança de design system em time distribuído",
    "B2B SaaS": "densidade de informação e decisão em produto B2B",
}


def _proof_from_fit(fit):
    for f in fit:
        if f in PROOF:
            return PROOF[f]
    return PROOF["Retail Media / AdTech"]


def _angle_from_fit(fit):
    for f in fit:
        if f in ANGLE:
            return ANGLE[f]
    return ANGLE["B2B SaaS"]


# ---------------------------------------------------------------- redação
def draft(tid, post_text):
    """Escreve 2 opções de comentário a partir do texto do post que você colou."""
    from agent import ask_json, EXAMPLES_FILE
    post_text = (post_text or "").strip()
    if len(post_text) < 40:
        raise ValueError("Cole o texto do post (pelo menos umas linhas). Sem ele o comentário "
                         "sai genérico, e genérico não gera conversa.")

    t = get(tid)
    if not t:
        raise ValueError("alvo não encontrado")

    profile = (PROMPTS / "profile.md").read_text() if (PROMPTS / "profile.md").exists() else ""
    style = (PROMPTS / "style.md").read_text() if (PROMPTS / "style.md").exists() else ""
    examples = EXAMPLES_FILE.read_text()[:4000] if EXAMPLES_FILE.exists() else ""

    ctx = f"Autor do post: {t['person'] or '(nome não identificado)'} — {t['role']}"
    if t["company"]:
        ctx += f" na {t['company']}"
    if t["job_title"]:
        ctx += (f"\nEssa empresa está com a vaga aberta: {t['job_title']} ({t['job_url']}). "
                "O comentário NÃO deve mencionar a vaga nem pedir nada — o objetivo é só "
                "que essa pessoa registre seu nome como alguém que pensa bem sobre o assunto.")

    prompt = f"""You write LinkedIn comments as the candidate, a Senior Product Designer.

# WHO RAMON IS
{profile}

# HIS VOICE
{style}

{("# HIS PAST POSTS (voice reference)\n" + examples) if examples else ""}

# THE PERSON HE IS COMMENTING ON
{ctx}
Angle that fits the candidate's experience here: {t['angle'] or 'his product design experience'}
Concrete proof he can draw on (use at most ONE, only if it genuinely fits): {t['proof']}

# THE POST HE IS COMMENTING ON
\"\"\"
{post_text[:4000]}
\"\"\"

# RULES
- Write in the SAME LANGUAGE as the post above.
- 250-600 characters. A comment, not a post.
- React to a SPECIFIC claim in the post. Quote or name the exact idea you're responding to.
- Add something the author does not already say: a counterpoint, a lived example, a sharper
  distinction, or a real number. If the candidate has nothing to add, say so in "verify".
- Never: "great post", "thanks for sharing", "totally agree", "this resonates", emoji strings,
  hashtags, self-promotion, asking for a job, mentioning the open role, tagging anyone.
- No bullet lists. Plain paragraphs, the way a person types in a comment box.
- Option A: substantive agreement that extends the idea with the candidate's experience.
- Option B: respectful friction — a tension, an exception, or a "it depends on X" the author missed.
- Do not invent facts about the candidate beyond the profile above.

Output ONLY this JSON:
{{"options": [{{"label": "A", "text": "..."}}, {{"label": "B", "text": "..."}}],
  "verify": "anything the candidate should double-check before posting, or empty string"}}"""

    data = ask_json(prompt)
    opts = data.get("options") or []
    if not opts:
        raise ValueError("o modelo não devolveu nenhuma opção")
    with _db() as con:
        con.execute("UPDATE targets SET options=?, post_text=? WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), post_text, tid))
    return data


# ---------------------------------------------------------------- estado
def get(tid):
    with _db() as con:
        r = con.execute("SELECT * FROM targets WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def today_queue():
    today = _now().date().isoformat()
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM targets WHERE substr(created_at,1,10)=? ORDER BY kind DESC, score DESC",
            (today,)).fetchall()
    return [dict(r) for r in rows]


def history(limit=60):
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM targets WHERE status!='queued' ORDER BY engaged_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_done(tid, comment, post_url=""):
    """Você comentou. Agenda o follow-up (convite com nota) pra alguns dias depois."""
    days = _cfg("engage_followup_days", 3)
    with _db() as con:
        con.execute("UPDATE targets SET status='engaged', comment=?, post_url=?, engaged_at=?,"
                    " followup_at=? WHERE id=?",
                    (comment, post_url, _now().isoformat(),
                     (_now() + timedelta(days=days)).isoformat(), tid))


def skip(tid, reason=""):
    with _db() as con:
        con.execute("UPDATE targets SET status='skipped', why=COALESCE(why,'')||?, engaged_at=?"
                    " WHERE id=?", (f" · pulado: {reason}" if reason else " · pulado", _now().isoformat(), tid))


def followups_due():
    """Quem você comentou há dias e ainda não convidou pra conectar."""
    with _db() as con:
        rows = con.execute(
            "SELECT * FROM targets WHERE status='engaged' AND followup_done=0"
            " AND followup_at IS NOT NULL AND followup_at <= ? ORDER BY followup_at",
            (_now().isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def mark_reply(tid):
    """O autor respondeu seu comentário. É o sinal mais direto de que abriu conversa,
    e o metrics.py usa esses comentários como exemplo do que funciona."""
    with _db() as con:
        con.execute("UPDATE targets SET got_reply=1 WHERE id=?", (tid,))


def followup_done(tid):
    with _db() as con:
        con.execute("UPDATE targets SET followup_done=1 WHERE id=?", (tid,))


def stats():
    with _db() as con:
        week = (_now() - timedelta(days=7)).isoformat()
        return {
            "engaged_week": con.execute(
                "SELECT count(*) FROM targets WHERE status='engaged' AND engaged_at>=?",
                (week,)).fetchone()[0],
            "followups_due": len(followups_due()),
            "jobs_pool": len(load_jobs()),
        }


# ---------------------------------------------------------------- rotina diária
def daily():
    """Chamado pelo scheduler: monta a fila e avisa."""
    from agent import notify, log
    try:
        n = build_queue()
        due = len(followups_due())
        msg = f"{n} pessoas pra comentar hoje"
        if due:
            msg += f" · {due} follow-up(s) vencendo"
        notify(msg, "Fila de engajamento")
    except Exception as e:
        log("engage.daily falhou:", e)
