#!/usr/bin/env python3
"""LinkedIn Agent — gera posts no seu estilo, pede aprovação no Mac e publica via API oficial.

Uso:
  python3 agent.py serve            # painel + agendador (é o que o launchd roda)
  python3 agent.py generate [tema]  # gera um rascunho agora
  python3 agent.py publish-due      # publica o que estiver vencido
  python3 agent.py engage           # monta a fila de engajamento do dia
"""
import csv
import io
import json
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import engage
import connect
import applications
import metrics

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"
DB_PATH = DATA / "agent.db"
APP_FILE = DATA / "linkedin_app.json"      # client_id / client_secret
TOKEN_FILE = DATA / "linkedin_token.json"  # access token + person urn
EXAMPLES_FILE = DATA / "examples.md"       # posts antigos (few-shot)
DATA.mkdir(exist_ok=True)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PILLAR_LABELS = {
    "case": "Bastidores de case", "ai": "Design AI-first", "adtech": "Retail Media & AdTech",
    "systems": "Design Systems & IA", "craft": "Produto + dados + eng.", "career": "Carreira & remoto",
}
_lock = threading.Lock()
JOBS = {"generating": 0, "last_error": ""}
OAUTH_STATE = {}


def cfg():
    return json.loads((ROOT / "config.json").read_text())


def tz():
    return ZoneInfo(cfg()["timezone"])


def now():
    return datetime.now(tz())


def log(*a):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- storage
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT, status TEXT, pillar TEXT, format TEXT, topic TEXT,
          text TEXT, first_comment TEXT, rationale TEXT, feedback TEXT,
          scheduled_at TEXT, published_at TEXT, linkedin_urn TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        engage.init(con)
        connect.init(con)
        applications.init(con)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(posts)")}
        for c in ("parent_urn", "source_url", "link_url", "link_title", "link_desc", "link_image"):
            if c not in cols:
                con.execute(f"ALTER TABLE posts ADD COLUMN {c} TEXT")
        metrics.init(con)   # depois do engage: mexe nas duas tabelas


def meta_get(key, default=None):
    with db() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def get_post(pid):
    with db() as con:
        r = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def update_post(pid, **fields):
    keys = ", ".join(f"{k}=?" for k in fields)
    with db() as con:
        con.execute(f"UPDATE posts SET {keys} WHERE id=?", (*fields.values(), pid))


def list_posts():
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 200")]
    for r in rows:   # preenche preview de link em background (não bloqueia /api/state)
        link = post_link(r)
        if link and r.get("link_url") != link:
            ensure_link_preview_async(r["id"])
    return rows


# ---------------------------------------------------------------- Link preview (Open Graph)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0 Safari/537.36")
_LP_INFLIGHT = set()
_LP_LOCK = threading.Lock()


def post_link(p):
    """Link do card: source_url (se http e não for repost) ou primeira URL http(s) do texto."""
    if p.get("parent_urn"):
        return None
    src = (p.get("source_url") or "").strip()
    if src.startswith("http") and "linkedin.com/" not in src:
        return src
    m = re.search(r"https?://[^\s<>\"')\]]+", p.get("text") or "")
    return m.group(0).rstrip(".,;:!?") if m else None


def fetch_og(url, timeout=8):
    import html as _html
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(600_000)
        cs = r.headers.get_content_charset() or "utf-8"
        final = r.geturl()
    doc = raw.decode(cs, errors="replace")
    meta = {}
    for tag in re.findall(r"<meta\b[^>]*>", doc, re.I):
        k = re.search(r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        v = re.search(r"content\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        if k and v:
            meta.setdefault(k.group(1).lower(), _html.unescape(v.group(1)).strip())
    title = meta.get("og:title") or meta.get("twitter:title")
    if not title:
        t = re.search(r"<title[^>]*>(.*?)</title>", doc, re.I | re.S)
        title = _html.unescape(re.sub(r"\s+", " ", t.group(1))).strip() if t else ""
    desc = meta.get("og:description") or meta.get("twitter:description") or meta.get("description") or ""
    img = meta.get("og:image") or meta.get("og:image:url") or meta.get("twitter:image") or ""
    if img:
        img = urllib.parse.urljoin(final, img)
    return {"title": title[:300], "desc": desc[:500], "image": img}


def refresh_link_preview(pid):
    p = get_post(pid)
    if not p:
        return None
    link = post_link(p)
    if not link:
        update_post(pid, link_url=None, link_title=None, link_desc=None, link_image=None)
        return None
    try:
        og = fetch_og(link)
    except Exception as e:
        log(f"link preview #{pid} falhou ({link}): {e}")
        og = {"title": "", "desc": "", "image": ""}
    update_post(pid, link_url=link, link_title=og["title"] or None,
                link_desc=og["desc"] or None, link_image=og["image"] or None)
    return og


def _lp_worker(pid):
    try:
        refresh_link_preview(pid)
    finally:
        with _LP_LOCK:
            _LP_INFLIGHT.discard(pid)


def ensure_link_preview_async(pid):
    with _LP_LOCK:
        if pid in _LP_INFLIGHT:
            return
        _LP_INFLIGHT.add(pid)
    threading.Thread(target=_lp_worker, args=(pid,), daemon=True).start()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_secret(path, obj):
    path.write_text(json.dumps(obj, indent=2))
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- generation
def pick_pillar_and_format(recent):
    c = cfg()
    weights = c["pillars"]
    last = [p["pillar"] for p in recent[:8]]
    # menor uso recente relativo ao peso; empate aleatório
    score = {k: (last.count(k) + 1 + (last[:1] == [k])) / w + random.random() * 0.2
             for k, w in weights.items() if w > 0}
    pillar = min(score, key=score.get)
    recent_formats = [p["format"] for p in recent[:3]]
    formats = [f for f in c["formats"] if f not in recent_formats] or c["formats"]
    return pillar, random.choice(formats)


def build_prompt(pillar, fmt, topic=None, feedback=None, previous=None, mode=None):
    c = cfg()
    with db() as con:
        recent = [dict(r) for r in con.execute(
            "SELECT pillar, format, topic, substr(text,1,220) AS hook, status FROM posts "
            "WHERE status IN ('published','scheduled','pending') ORDER BY id DESC LIMIT 15")]
        rejected = [dict(r) for r in con.execute(
            "SELECT substr(text,1,160) AS hook, feedback FROM posts "
            "WHERE feedback IS NOT NULL AND feedback != '' ORDER BY id DESC LIMIT 10")]
    examples = EXAMPLES_FILE.read_text() if EXAMPLES_FILE.exists() else "(no past posts imported yet)"
    parts = [
        "You are the candidate's LinkedIn ghostwriter. Write ONE LinkedIn post in his voice.",
        f"# PROFILE (the ONLY source of facts)\n{(PROMPTS / 'profile.md').read_text()}",
        f"# STRATEGY\n{(PROMPTS / 'strategy.md').read_text()}",
        f"# VOICE & STYLE\n{(PROMPTS / 'style.md').read_text()}",
        f"# PAST POSTS BY RAMON (match this voice; do not copy)\n{examples[:12000]}",
        "# RECENTLY WRITTEN (do NOT repeat these topics/hooks)\n" +
        ("\n".join(f"- [{r['pillar']}/{r['format']}] {r['topic']}: {r['hook']!r}" for r in recent) or "(none)"),
        "# FEEDBACK RAMON GAVE ON PAST DRAFTS (learn from it)\n" +
        ("\n".join(f"- On {r['hook']!r}: {r['feedback']}" for r in rejected) or "(none)"),
        # Desempenho real dos posts já publicados. Vem depois do feedback de rascunho
        # de propósito: número medido pesa mais que opinião sobre um texto não publicado.
        metrics.prompt_section(),
        f"# THIS POST\n- Pillar: {pillar}\n- Format: {fmt}\n- Language: {c['language']}\n"
        f"- Topic: {topic or 'choose the strongest fresh angle for this pillar'}\n"
        f"- Today: {now():%A %Y-%m-%d}\n- Portfolio URL for first_comment (only if case/teardown): {c['portfolio_url']}",
    ]
    if previous:
        parts.append(f"# PREVIOUS DRAFT TO REWRITE\n{previous}\n\n# RAMON'S NOTES ON IT\n{feedback or 'Make it better.'}")
    if mode == "take":
        src = (feedback or "").strip()
        if src.lower() == "auto":
            src = ""
        parts.append(
            "# REPOST WITH THOUGHTS (NOT a long-form post)\n"
            "the candidate is resharing someone else's LinkedIn post to his feed and adding his own thoughts on top. "
            "The original post will be embedded below his words, and his readers can see it. "
            "Write ONLY his commentary, the text that sits above the reshared post.\n"
            "Rules for this format:\n"
            "- 350-900 characters. Much shorter than a normal post. No storytelling arc.\n"
            "- Take a clear stance in the FIRST line: agree and extend, disagree, or add the missing nuance. "
            "Never a neutral summary, never 'great post!'.\n"
            "- Earn the take with ONE concrete detail from his own experience (a number, a decision, a constraint). "
            "One is enough; do not retell a whole case.\n"
            "- Do not name or tag the original author, and do not quote them at length. Refer to the idea, not the person.\n"
            "- End with a short question or a sharp closing line. 0-2 hashtags.\n"
            "- first_comment must be empty for this format.")
        if src:
            parts.append(f"# THE POST HE IS RESHARING (react to THIS)\n{src[:4000]}")
        elif topic and topic.startswith("http"):
            parts.append(
                f"# HE IS RESHARING THIS LINKEDIN POST, BUT YOU CANNOT READ IT\n{topic}\n"
                "LinkedIn blocks automated reading. Infer the subject from the URL slug (the hashtag-like words in it) "
                "and confirm it with WebSearch. Write a take about that SUBJECT, phrased so it still reads correctly "
                "even if the original post's exact argument differs — never put words in the author's mouth, never "
                "start with 'This post says…'. In 'verify', state plainly what you assumed the post is about so candidato "
                "can check it against the real post before publishing. If the slug is opaque and search finds "
                "nothing, do NOT explain that and do NOT refuse: write thoughts on the broader theme the candidate cares "
                "about that any post from this author plausibly touches, keep them true regardless of the original, "
                "and say in 'verify' that the original post could not be read at all.")

    elif mode == "trend" and not topic:
        parts.append(
            "# TRENDING MODE (this is the candidate's morning draft)\nUse WebSearch (2-4 searches) to find what the tech/product "
            "community on LinkedIn and in tech news is discussing THIS WEEK: AI products and agents, design tools, "
            "product management, retail media/AdTech, remote work in tech, big launches. Pick the ONE trend where the candidate "
            "has real, credible expertise and write his take on it, anchored in his experience from PROFILE. "
            "You may switch pillar/format if the trend fits another one better. Only use facts about the trend "
            "that you actually found. No URLs in the post. Name the trend in 'trend'.")
    elif mode == "followup":
        last = latest_published()
        if last:
            parts.append(
                f"# RAMON JUST PUBLISHED THIS\n{last['text']}\n\nWrite the NEXT post. Decide what is stronger: "
                "(a) a natural continuation (part 2, a deeper cut, the counter-argument, the 'how' behind it) or "
                "(b) a completely different subject from another pillar. Say which in 'rationale'.")
    elif c.get("use_web_search") and pillar in ("ai", "adtech", "craft") and not previous:
        parts.append("You MAY do one quick web search for a fresh, real, verifiable news hook from the last 30 days "
                     "relevant to this pillar. Only use it if it is real and adds value; cite no URLs in the post.")
    parts.append(
        "# OUTPUT\nReturn ONLY a JSON object, no prose, no markdown fences:\n"
        '{"topic": "<5-8 word topic label>", "post": "<the full post text with \\n line breaks>", '
        '"first_comment": "<optional first comment, or empty string>", '
        '"rationale": "<1-2 sentences in Portuguese: why this post should drive profile views from recruiters>", '
        '"verify": "<in Portuguese: any claim, anecdote or feeling in the post NOT literally in PROFILE that the candidate '
        'must confirm is true; empty string if none>", '
        '"pillar": "<pillar id used>", "format": "<format used>", "trend": "<trending topic used, or empty>", '
        '"next_ideas": [{"title": "<idea in Portuguese, max 12 words>", "angle": "<1 sentence in Portuguese>", '
        '"kind": "continuação|em alta|evergreen"}, <exactly 3 ideas for the posts AFTER this one; at least one '
        'continuation of this post and one trend if you know any>]}\n'
        "Before answering, silently check: hook <= 200 chars and creates tension; 700-1600 chars; every number "
        "exists in PROFILE; no buzzwords; ends with question or takeaway.")
    return "\n\n".join(p for p in parts if p)   # seções vazias (ex.: métricas sem dado) somem


def run_claude(prompt, allow_web=False, timeout=420):
    c = cfg()
    cmd = ["claude", "-p", "--output-format", "text", "--no-session-persistence"]
    if c.get("claude_model"):
        cmd += ["--model", c["claude_model"]]
    if c.get("claude_effort"):
        cmd += ["--effort", c["claude_effort"]]
    if allow_web:
        cmd += ["--allowedTools", "WebSearch", "WebFetch"]
    env = dict(os.environ)
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "/usr/bin:/bin")
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=DATA, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"claude CLI falhou ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}"[:800])
    return p.stdout.strip()


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def resolve_short_url(url, hops=5):
    """Segue redirects de lnkd.in / encurtadores até a URL real do post."""
    for _ in range(hops):
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host.endswith("linkedin.com"):
            return url
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            r = urllib.request.build_opener(NoRedirect).open(req, timeout=20)
            loc = r.headers.get("Location")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
        except Exception as e:
            raise ValueError(f"Não consegui abrir esse link ({e}). Cole o link completo do post.")
        if not loc:
            return url
        url = urllib.parse.urljoin(url, loc)
    return url


def parse_activity_urn(url_or_urn):
    """Extrai a URN do post (share/ugcPost/groupPost preservados; senão activity) de uma URL ou URN."""
    s = urllib.parse.unquote((url_or_urn or "").strip())
    if not s:
        return None
    urn_re = r"urn:li:(activity|share|ugcPost|groupPost):(\d+)"
    m = re.search(urn_re, s)
    if m:
        return f"urn:li:{m.group(1)}:{m.group(2)}"
    if s.startswith("http") and "linkedin.com" not in urllib.parse.urlparse(s).netloc.lower():
        s = urllib.parse.unquote(resolve_short_url(s))
        m = re.search(urn_re, s)
        if m:
            return f"urn:li:{m.group(1)}:{m.group(2)}"
    path = urllib.parse.urlparse(s).path if s.startswith("http") else s
    m = re.search(r"-(activity|share|ugcPost)-(\d{15,25})", path)   # /posts/…-activity-NNN-xxxx
    if m:
        return f"urn:li:{m.group(1)}:{m.group(2)}"
    m = re.search(r"(\d{15,25})", path)
    if m:
        return f"urn:li:activity:{m.group(1)}"
    raise ValueError("Não achei o ID do post nesse link. Abra o post, clique em ••• → Copiar link da publicação "
                     "e cole o link completo (linkedin.com/posts/… ou /feed/update/…).")


def latest_published():
    with db() as con:
        r = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY published_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def pending_count():
    with db() as con:
        return con.execute("SELECT count(*) FROM posts WHERE status='pending'").fetchone()[0]


def morning_brief():
    """Garante que às ~9h30 exista um rascunho esperando e mostra o popup."""
    with db() as con:
        r = con.execute("SELECT id FROM posts WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    if r:
        notify("Bom dia! Seu rascunho de hoje está pronto.")
        approval_popup(r["id"])
    elif JOBS["generating"] == 0 and cfg().get("auto_generate_scheduled"):
        generate(mode="trend")
    elif JOBS["generating"] == 0:
        log("morning_brief: auto_generate_scheduled=false — sem gerar")


def after_publish():
    """Logo depois de publicar: deixa o próximo rascunho pronto (se auto_draft_after_publish)."""
    if not cfg().get("auto_draft_after_publish"):
        log("after_publish: auto_draft_after_publish=false — sem gerar")
        return
    if pending_count() >= cfg()["max_pending_drafts"] or JOBS["generating"]:
        return
    try:
        pid = generate(mode="followup", notify=False)
        notify("Próximo rascunho já está pronto no painel.", get_post(pid)["topic"] or "")
    except Exception:
        pass


def rationale_with_verify(data):
    r = data.get("rationale", "")
    if (data.get("verify") or "").strip():
        r += "\n⚠️ Confira: " + data["verify"].strip()
    return r


def parse_json(out):
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"Resposta sem JSON: {out[:300]}")
    return json.loads(m.group(0))


def ask_json(prompt, allow_web=False):
    """Roda o Claude e insiste se ele responder em prosa em vez de JSON."""
    out = run_claude(prompt, allow_web=allow_web)
    try:
        return parse_json(out)
    except Exception as e:
        log("resposta inválida, tentando de novo:", e)
        retry = (prompt + "\n\n# RETRY — YOUR LAST ANSWER WAS REJECTED\nYou replied with prose instead of JSON:\n"
                 + out[:600] + "\n\nNever refuse, never explain, never ask for more input. If information is "
                 "missing, write the best post you can with what you have and record the limitation in 'verify'. "
                 "Output the JSON object and nothing else.")
        return parse_json(run_claude(retry, allow_web=False))


def generate(topic=None, pillar=None, fmt=None, notify=True, mode=None, source=None, url=None):
    """Gera um rascunho novo (status=pending) e dispara o popup de aprovação."""
    JOBS["generating"] += 1
    try:
        with db() as con:
            recent = [dict(r) for r in con.execute("SELECT pillar, format FROM posts ORDER BY id DESC LIMIT 10")]
        auto_p, auto_f = pick_pillar_and_format(recent)
        pillar, fmt = pillar or auto_p, fmt or auto_f
        log(f"gerando: pillar={pillar} format={fmt} topic={topic!r}")
        if url:
            url = resolve_short_url(url) if url.startswith("http") else url
        parent = parse_activity_urn(url) if url else None
        if mode == "take":
            fmt = "take"
        prompt = build_prompt(pillar, fmt, topic or (url if mode == "take" else None),
                              feedback=source if mode == "take" else None, mode=mode)
        web = cfg().get("use_web_search") and (mode in ("trend", "take") or pillar in ("ai", "adtech", "craft"))
        data = ask_json(prompt, allow_web=web)
        if data.get("pillar") in cfg()["pillars"]:
            pillar = data["pillar"]
        if mode != "take" and data.get("format") in cfg()["formats"]:
            fmt = data["format"]
        if data.get("trend"):
            data["rationale"] = f"🔥 Em alta: {data['trend']}\n" + data.get("rationale", "")
        if isinstance(data.get("next_ideas"), list):
            meta_set("next_ideas", json.dumps(data["next_ideas"][:3], ensure_ascii=False))
        with db() as con:
            cur = con.execute(
                "INSERT INTO posts (created_at,status,pillar,format,topic,text,first_comment,rationale,"
                "parent_urn,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now().isoformat(), "pending", pillar, fmt, data.get("topic") or (topic if not (topic or "").startswith("http") else "") or "",
                 data["post"].strip(), "" if mode == "take" else (data.get("first_comment") or "").strip(),
                 rationale_with_verify(data), parent, (url or "").strip() or None))
            pid = cur.lastrowid
        log(f"rascunho #{pid} criado")
        try:
            refresh_link_preview(pid)
        except Exception as e:
            log("link preview:", e)
        JOBS["last_error"] = ""
        if notify and cfg().get("popup_on_new_draft"):
            threading.Thread(target=approval_popup, args=(pid,), daemon=True).start()
        return pid
    except Exception as e:
        JOBS["last_error"] = str(e)
        log("erro gerando:", e)
        raise
    finally:
        JOBS["generating"] -= 1


def regenerate(pid, feedback):
    JOBS["generating"] += 1
    try:
        p = get_post(pid)
        prompt = build_prompt(p["pillar"], p["format"], p["topic"], feedback=feedback, previous=p["text"])
        data = ask_json(prompt)
        update_post(pid, text=data["post"].strip(), first_comment=(data.get("first_comment") or "").strip(),
                    rationale=rationale_with_verify(data), topic=data.get("topic") or p["topic"],
                    feedback=feedback, status="pending", error=None)
        JOBS["last_error"] = ""
    except Exception as e:
        JOBS["last_error"] = str(e)
        log("erro regenerando:", e)
    finally:
        JOBS["generating"] -= 1


def learn_style(raw_posts):
    """Salva posts antigos como exemplos e reescreve prompts/style.md a partir deles."""
    posts = [p.strip() for p in raw_posts if p and len(p.strip()) > 80]
    if not posts:
        raise ValueError("Nenhum post com conteúdo suficiente encontrado.")
    best = sorted(posts, key=len, reverse=True)[:10]
    EXAMPLES_FILE.write_text("\n\n---\n\n".join(best))
    prompt = (
        "Below are real LinkedIn posts written by the candidate (Senior Product Designer). "
        "Analyze them and write a concise VOICE & STYLE GUIDE (markdown, max 45 lines) that another writer could "
        "follow to sound exactly like him: language(s) used, tone, sentence length, how he opens (hook patterns), "
        "structure, formatting (line breaks, lists, emojis, hashtags), how he closes/CTA, recurring themes, "
        "vocabulary he likes and avoids. Then add a section 'Upgrades for recruiter visibility' with 3-5 changes "
        "that keep his voice but increase profile views from recruiters. Output only the markdown.\n\n"
        + "\n\n---\n\n".join(posts[:40])[:40000])
    guide = run_claude(prompt)
    (PROMPTS / "style.md").write_text(f"# Voice & style — aprendido de {len(posts)} posts ({now():%Y-%m-%d})\n\n{guide}\n")
    return len(posts)


def parse_imported(text):
    """Aceita Shares.csv do export do LinkedIn ou posts colados separados por ---."""
    if "ShareCommentary" in text[:500]:
        rows = csv.DictReader(io.StringIO(text))
        return [r.get("ShareCommentary", "").replace('""', '"') for r in rows]
    return re.split(r"\n\s*-{3,}\s*\n", text)


# ---------------------------------------------------------------- scheduling
def occurrences(spec_list, ref, back=False):
    """Para cada 'ddd HH:MM', devolve a próxima (ou última, se back) ocorrência relativa a ref."""
    out = []
    for spec in spec_list:
        d, hm = spec.split()
        h, m = map(int, hm.split(":"))
        delta = DAYS.index(d.lower()) - ref.weekday()
        t = (ref + timedelta(days=delta)).replace(hour=h, minute=m, second=0, microsecond=0)
        if back:
            t = t - timedelta(days=7) if t > ref else t
        else:
            t = t + timedelta(days=7) if t <= ref else t
        out.append((spec, t))
    return out


def next_free_slot():
    with db() as con:
        taken = {r["scheduled_at"] for r in con.execute("SELECT scheduled_at FROM posts WHERE status='scheduled'")}
    ref = now()
    for week in range(8):
        for _, t in sorted(occurrences(cfg()["publish_slots"], ref + timedelta(days=7 * week)), key=lambda x: x[1]):
            if t > now() and t.isoformat() not in taken:
                return t
    return now() + timedelta(minutes=1)


def approve(pid, text=None, first_comment=None, when="slot"):
    p = get_post(pid)
    if not p:
        raise ValueError("post não encontrado")
    fields = {}
    if text is not None:
        fields["text"] = text
    if first_comment is not None:
        fields["first_comment"] = first_comment
    if when == "now":
        fields.update(status="scheduled", scheduled_at=now().isoformat(), error=None)
        update_post(pid, **fields)
        return publish_post(pid)
    if when == "slot":
        t = next_free_slot()
    elif when == "later":
        t = (now() + timedelta(hours=cfg().get("later_hours", 6))).replace(second=0, microsecond=0)
    else:
        t = datetime.fromisoformat(when).astimezone(tz())
    fields.update(status="scheduled", scheduled_at=t.isoformat(), error=None)
    update_post(pid, **fields)
    notify(f"Agendado para {t:%a %d/%m %H:%M}", p["topic"] or "Post aprovado")
    return t.isoformat()


def publish_due():
    with db() as con:
        due = [r["id"] for r in con.execute("SELECT id, scheduled_at FROM posts WHERE status='scheduled'")
               if datetime.fromisoformat(r["scheduled_at"]) <= now()]
    for pid in due:
        try:
            publish_post(pid)
        except Exception as e:
            log(f"falha publicando #{pid}:", e)


def scheduler_loop():
    while True:
        try:
            publish_due()
            c = cfg()
            for spec, t in occurrences(c["generate_at"], now(), back=True):
                key = f"gen:{spec}:{t:%Y-%m-%d}"
                if meta_get(key) or now() - t > timedelta(hours=12):
                    continue
                meta_set(key, now().isoformat())
                threading.Thread(target=morning_brief, daemon=True).start()
            for spec, t in occurrences(c.get("engage_at", []), now(), back=True):
                key = f"engage:{spec}:{t:%Y-%m-%d}"
                if meta_get(key) or now() - t > timedelta(hours=12):
                    continue
                meta_set(key, now().isoformat())
                if c.get("auto_engage_daily"):
                    threading.Thread(target=engage.daily, daemon=True).start()
                if c.get("auto_connect_daily"):
                    threading.Thread(target=connect.daily, daemon=True).start()
                threading.Thread(target=metrics.remind, daemon=True).start()
            check_token_expiry()
        except Exception:
            log(traceback.format_exc())
        time.sleep(60)


# ---------------------------------------------------------------- macOS UI
def osascript(lines, *args, timeout=None):
    cmd = ["osascript"]
    for ln in lines:
        cmd += ["-e", ln]
    cmd += ["--", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip()


def notify(msg, title="LinkedIn Agent"):
    try:
        osascript(["on run argv", "display notification (item 1 of argv) with title \"LinkedIn Agent\" "
                   "subtitle (item 2 of argv) sound name \"Glass\"", "end run"], msg, title)
    except Exception as e:
        log("notify falhou:", e)


def open_dashboard(anchor=""):
    subprocess.run(["open", f"http://localhost:{cfg()['port']}/{anchor}"])


def approval_popup(pid):
    """Popup nativo: Aprovar / Revisar / Rejeitar."""
    p = get_post(pid)
    if not p or p["status"] != "pending":
        return
    body = p["text"]
    preview = body if len(body) <= 900 else body[:900].rstrip() + "…"
    header = f"{PILLAR_LABELS.get(p['pillar'], p['pillar'])} · {p['format']} · {len(body)} caracteres\n\n"
    with _lock:  # um popup por vez
        try:
            out = osascript([
                "on run argv",
                "set r to display dialog (item 1 of argv) with title \"LinkedIn Agent — novo rascunho\" "
                "buttons {\"Rejeitar\", \"Revisar / editar\", \"Aprovar\"} default button \"Aprovar\" "
                "with icon note giving up after 21600",
                "return (button returned of r) & \"|\" & (gave up of r)",
                "end run"], header + preview, timeout=21700)
        except Exception as e:
            log("popup falhou:", e)
            notify("Novo rascunho pronto para revisão", p["topic"] or "")
            return
    if get_post(pid)["status"] != "pending":  # já tratado pelo painel
        return
    button = out.split("|")[0]
    if button == "Aprovar":
        h = cfg().get("later_hours", 6)
        later = now() + timedelta(hours=h)
        slot = next_free_slot()
        when = osascript([
            "on run argv",
            "set r to display dialog \"Quando publicar?\" with title \"LinkedIn Agent\" buttons "
            "{(item 1 of argv), (item 2 of argv), \"Publicar agora\"} default button (item 2 of argv)",
            "return button returned of r",
            "end run"], f"Próximo horário ({slot:%a %H:%M})", f"Daqui a {h}h ({later:%H:%M})", timeout=3600)
        try:
            approve(pid, when="now" if when == "Publicar agora" else "later" if when.startswith("Daqui") else "slot")
        except Exception as e:
            notify(f"Erro ao aprovar: {e}")
    elif button == "Revisar / editar":
        open_dashboard(f"#post-{pid}")
    elif button == "Rejeitar":
        reason = osascript([
            "on run argv",
            "set r to display dialog \"O que não funcionou? (opcional — o agente aprende com isso)\" "
            "default answer \"\" with title \"LinkedIn Agent\" buttons {\"Pular\", \"Enviar\"} default button \"Enviar\"",
            "return text returned of r",
            "end run"], timeout=3600)
        update_post(pid, status="rejected", feedback=reason or "rejected without comment")


# ---------------------------------------------------------------- LinkedIn API
def api_version():
    v = cfg().get("linkedin_api_version")
    if v:
        return v
    d = now().replace(day=1) - timedelta(days=45)  # ~2 meses atrás: sempre dentro da janela suportada
    return f"{d:%Y%m}"


def http(method, url, body=None, headers=None, form=False):
    data = None
    headers = dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, dict(r.headers), json.loads(raw) if raw.strip().startswith("{") else {}
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), {"error": e.read().decode(errors="replace")}


def redirect_uri():
    return f"http://localhost:{cfg()['port']}/callback"


def linkedin_status():
    app = read_json(APP_FILE, {})
    tok = read_json(TOKEN_FILE, {})
    exp = tok.get("expires_at")
    return {
        "app_configured": bool(app.get("client_id")),
        "connected": bool(tok.get("access_token")) and (not exp or datetime.fromisoformat(exp) > now()),
        "name": tok.get("name"), "expires_at": exp, "redirect_uri": redirect_uri(),
    }


def check_token_expiry():
    tok = read_json(TOKEN_FILE, {})
    if not tok.get("expires_at"):
        return
    left = datetime.fromisoformat(tok["expires_at"]) - now()
    key = f"warn-expiry:{tok['expires_at'][:10]}"
    if left < timedelta(days=5) and not meta_get(key):
        meta_set(key, "1")
        notify("Seu acesso ao LinkedIn expira em breve — reconecte no painel.")


def little_text(s):
    """Escapa o formato 'little text' do Posts API, preservando hashtags."""
    tags = []

    def keep(m):
        tags.append(m.group(1))
        return f"\x00{len(tags) - 1}\x00"
    s = re.sub(r"#(\w+)", keep, s)
    s = re.sub(r"([\\|{}@\[\]()<>#*_~])", r"\\\1", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: "{hashtag|\\#|" + tags[int(m.group(1))] + "}", s)


def upload_thumbnail(img_url, author, h):
    """Sobe a og:image via Images API (initializeUpload) e devolve urn:li:image; None se falhar."""
    if not img_url:
        return None
    try:
        req = urllib.request.Request(img_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read(8_000_000)
            ctype = r.headers.get_content_type()
        if not ctype.startswith("image/"):
            return None
        st, _, b = http("POST", "https://api.linkedin.com/rest/images?action=initializeUpload",
                        {"initializeUploadRequest": {"owner": author}}, h)
        v = (b or {}).get("value") or {}
        if st not in (200, 201) or not v.get("uploadUrl"):
            log(f"thumbnail initializeUpload {st}: {str(b)[:200]}")
            return None
        put = urllib.request.Request(v["uploadUrl"], data=data, method="PUT", headers={
            "Authorization": h["Authorization"], "Content-Type": ctype})
        with urllib.request.urlopen(put, timeout=30):
            pass
        return v.get("image")
    except Exception as e:
        log("thumbnail upload falhou:", e)
        return None


def publish_post(pid):
    p = get_post(pid)
    tok = read_json(TOKEN_FILE, {})
    if not tok.get("access_token"):
        update_post(pid, error="LinkedIn não conectado")
        notify("Não publiquei: conecte o LinkedIn no painel.", p["topic"] or "")
        raise RuntimeError("LinkedIn não conectado")
    h = {"Authorization": f"Bearer {tok['access_token']}", "X-Restli-Protocol-Version": "2.0.0",
         "LinkedIn-Version": api_version()}
    author = tok["person_urn"]
    parent = p.get("parent_urn")
    # /rest/posts não aceita urn:li:activity como parent; o ID costuma ser o mesmo do share/ugcPost.
    candidates = [parent]
    if parent and parent.startswith("urn:li:activity:"):
        n = parent.rsplit(":", 1)[1]
        candidates = [f"urn:li:share:{n}", f"urn:li:ugcPost:{n}"]
    link = post_link(p) if not parent else None
    if link and p.get("link_url") != link:
        try:
            refresh_link_preview(pid)
            p = get_post(pid)
        except Exception as e:
            log("link preview:", e)
    article = None
    if link:
        article = {"source": link, "title": (p.get("link_title") or link)[:400]}
        if p.get("link_desc"):
            article["description"] = p["link_desc"][:4000]
        thumb = upload_thumbnail(p.get("link_image"), author, h)
        if thumb:
            article["thumbnail"] = thumb
    for cand in candidates:
        status, headers, body = http("POST", "https://api.linkedin.com/rest/posts", {
            **({"content": {"article": article}} if article else {}),
            "author": author, "commentary": little_text(p["text"]), "visibility": "PUBLIC",
            **({"reshareContext": {"parent": cand}} if cand else {}),
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}, h)
        if status in (200, 201) or not parent:
            break
        log(f"repost com parent {cand} falhou: {status} {str(body)[:200]}")
    if status not in (200, 201) and parent:
        err = f"HTTP {status}: {str(body)[:400]}"
        update_post(pid, status="failed", error=(
            "Repost falhou: não encontrei uma referência share/ugcPost válida para esse post "
            f"(tentei {', '.join(candidates)}). O post pode estar disponível — copie de novo o link pelo "
            "menu ••• → Copiar link da publicação e tente outra vez. " + err))
        notify("Falha ao repostar — veja o painel.", p["topic"] or "")
        raise RuntimeError(err)
    if status not in (200, 201):
        log(f"/rest/posts {status}: {body}; tentando ugcPosts")
        status, headers, body = http("POST", "https://api.linkedin.com/v2/ugcPosts", {
            "author": author, "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": p["text"]},
                **({"shareMediaCategory": "ARTICLE", "media": [{
                    "status": "READY", "originalUrl": link,
                    "title": {"text": article["title"]},
                    **({"description": {"text": article["description"]}} if article.get("description") else {})}]}
                   if article else {"shareMediaCategory": "NONE"})}},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}},
            {k: v for k, v in h.items() if k != "LinkedIn-Version"})
    if status not in (200, 201):
        err = f"HTTP {status}: {str(body)[:500]}"
        update_post(pid, status="failed", error=err)
        notify("Falha ao publicar — veja o painel.", p["topic"] or "")
        raise RuntimeError(err)
    urn = headers.get("x-restli-id") or headers.get("X-RestLi-Id") or body.get("id", "")
    update_post(pid, status="published", published_at=now().isoformat(), linkedin_urn=urn, error=None)
    log(f"#{pid} publicado: {urn}")
    if p.get("first_comment"):
        time.sleep(20)
        c_status, _, c_body = http("POST", f"https://api.linkedin.com/rest/socialActions/{urllib.parse.quote(urn, safe='')}/comments",
                                   {"actor": author, "object": urn, "message": {"text": p["first_comment"]}}, h)
        if c_status not in (200, 201):
            log(f"comentário falhou {c_status}: {c_body}")
            update_post(pid, error=f"Post publicado, mas o 1º comentário falhou (HTTP {c_status}) — comente manualmente.")
    notify("Publicado no LinkedIn ✓", p["topic"] or "")
    threading.Thread(target=after_publish, daemon=True).start()
    return urn


def oauth_url():
    app = read_json(APP_FILE, {})
    state = secrets.token_urlsafe(16)
    OAUTH_STATE[state] = time.time()
    return "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": app["client_id"], "redirect_uri": redirect_uri(),
        "state": state, "scope": "openid profile w_member_social"})


def oauth_callback(code, state):
    if state not in OAUTH_STATE:
        raise ValueError("state inválido — tente conectar de novo")
    OAUTH_STATE.pop(state)
    app = read_json(APP_FILE, {})
    status, _, body = http("POST", "https://www.linkedin.com/oauth/v2/accessToken", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "client_id": app["client_id"], "client_secret": app["client_secret"]}, form=True)
    if status != 200:
        raise RuntimeError(f"Troca de token falhou: {body}")
    token = body["access_token"]
    s2, _, me = http("GET", "https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {token}"})
    if s2 != 200:
        raise RuntimeError(f"userinfo falhou: {me}")
    write_secret(TOKEN_FILE, {
        "access_token": token, "person_urn": f"urn:li:person:{me['sub']}", "name": me.get("name"),
        "expires_at": (now() + timedelta(seconds=int(body.get("expires_in", 5184000)))).isoformat()})


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/":
            return self._send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html")
        if u.path == "/api/state":
            return self._send(200, {
                "posts": list_posts(), "linkedin": linkedin_status(), "jobs": JOBS,
                "next_slot": next_free_slot().isoformat(), "pillars": PILLAR_LABELS,
                "config": {k: cfg()[k] for k in ("publish_slots", "generate_at", "language", "timezone")},
                "style_learned": EXAMPLES_FILE.exists(), "later_hours": cfg().get("later_hours", 6),
                "ideas": json.loads(meta_get("next_ideas", "[]"))})
        if u.path == "/api/metrics":
            return self._send(200, {"summary": metrics.summary(), "pending": metrics.pending()})
        if u.path == "/api/engage":
            return self._send(200, {
                "queue": engage.today_queue(), "followups": engage.followups_due(),
                "history": engage.history(), "stats": engage.stats()})
        if u.path == "/api/applications":
            return self._send(200, {"rows": applications.list_all(), "sheet_url": cfg().get("sheet_url", ""),
                                    "pending": applications.tsv_pending()[1],
                                    "sheet": applications.sheet_status()})
        if u.path == "/api/applications/tsv":
            return self._send(200, {"tsv": applications.tsv_pending()[0], "count": applications.tsv_pending()[1]})
        if u.path == "/api/jobs":
            import jobfeedback
            gone = jobfeedback.rejected_urls()
            hidden = {}
            rows = connect.jobs(hidden)
            import want
            wanted = want.wanted_urls()
            for j in rows:
                j["wanted"] = j.get("url") in wanted
            return self._send(200, {"jobs": [j for j in rows if j.get("url") not in gone], "hidden": hidden,
                                    "rejected": sum(1 for j in rows if j.get("url") in gone)})
        if u.path == "/api/want":
            import want
            return self._send(200, want.state())
        if u.path == "/api/want/daily":
            import want
            return self._send(200, {"text": want.daily_text()})
        if u.path == "/api/job-text":
            return self._send(200, connect.job_text(q.get("id", "")))
        if u.path == "/api/collect-status":
            return self._send(200, connect.collect_status())
        if u.path == "/api/jobfeedback":
            import jobfeedback
            return self._send(200, {"reasons": jobfeedback.reasons_catalog(), "patterns": jobfeedback.patterns()})
        if u.path == "/api/connect":
            return self._send(200, connect.state())
        if u.path == "/auth/linkedin":
            if not read_json(APP_FILE, {}).get("client_id"):
                return self._send(400, "Configure o app do LinkedIn no painel primeiro.", "text/plain")
            self.send_response(302)
            self.send_header("Location", oauth_url())
            return self.end_headers()
        if u.path == "/callback":
            try:
                if "error" in q:
                    raise RuntimeError(q.get("error_description", q["error"]))
                oauth_callback(q["code"], q.get("state", ""))
                self.send_response(302)
                self.send_header("Location", "/#connected")
                return self.end_headers()
            except Exception as e:
                return self._send(400, f"Erro conectando ao LinkedIn: {e}", "text/plain")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # header customizado força preflight CORS → sites externos não conseguem chamar a API local
        if not self._local_only() or self.headers.get("X-Agent") != "1":
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        parts = self.path.strip("/").split("/")
        try:
            if parts == ["api", "generate"]:
                if JOBS["generating"]:
                    return self._send(409, {"error": "Já estou gerando um rascunho, aguarde."})
                threading.Thread(target=generate, kwargs={
                    "topic": body.get("topic") or None, "pillar": body.get("pillar") or None,
                    "notify": False, "mode": body.get("mode"),
                    "source": body.get("source"), "url": body.get("url")}, daemon=True).start()
                return self._send(202, {"ok": True})
            if parts == ["api", "take"]:
                if JOBS["generating"]:
                    return self._send(409, {"error": "Já estou escrevendo, aguarde."})
                url = (body.get("url") or "").strip()
                src = (body.get("source") or "").strip()
                if src.lower() == "auto":
                    src = ""
                if not url:
                    return self._send(400, {"error": "Um repost with thoughts precisa do link do post original "
                                                     "(••• → Copiar link da publicação)."})
                parse_activity_urn(url)  # valida antes de gastar uma geração
                threading.Thread(target=generate, kwargs={
                    "mode": "take", "url": url or None, "source": src or None,
                    "pillar": body.get("pillar") or None, "notify": False}, daemon=True).start()
                return self._send(202, {"ok": True})
            if parts == ["api", "metrics", "import"]:
                return self._send(200, {"ok": True, "count": metrics.import_csv(body.get("text", ""))})
            if parts == ["api", "engage", "build"]:
                return self._send(200, {"ok": True, "count": engage.build_queue()})
            if parts == ["api", "applications"]:
                return self._send(200, {"ok": True, "id": applications.add(body)})
            if parts == ["api", "applications", "sheet"]:
                applications.save_url(body.get("url", ""))
                if body.get("url"):
                    applications._call({"action": "ping"})
                return self._send(200, {"ok": True})
            if parts == ["api", "applications", "sync"]:
                return self._send(200, {"ok": True, **applications.sync()})
            if parts == ["api", "applications", "synced"]:
                return self._send(200, {"ok": True, "count": applications.tsv_pending(mark=True)[1]})
            if len(parts) == 4 and parts[:2] == ["api", "applications"]:
                aid = int(parts[2])
                if parts[3] == "update":
                    applications.update(aid, body)
                elif parts[3] == "delete":
                    applications.delete(aid)
                else:
                    return self._send(404, {"error": "ação desconhecida"})
                return self._send(200, {"ok": True})
            if parts == ["api", "jobs", "reject"]:
                import jobfeedback
                jobfeedback.reject(body.get("job") or {}, body.get("reasons") or [], body.get("note") or "")
                return self._send(200, {"ok": True})
            if parts[:2] == ["api", "want"] and len(parts) == 3:
                import want
                a = parts[2]
                if a == "mark":
                    want.mark(body.get("job") or {}, body.get("on", True))
                elif a == "find":
                    return self._send(200, want.find(body.get("url") or ""))
                elif a == "add":
                    want.add_manual(body.get("url"), body.get("profile"), body.get("name"), body.get("role"))
                elif a == "act":
                    want.act(int(body.get("id")), body.get("action") or "")
                else:
                    return self._send(404, {"error": "ação desconhecida"})
                return self._send(200, {"ok": True})
            if parts == ["api", "jobs", "rule"]:
                import jobfeedback
                if body.get("action") == "approve":
                    return self._send(200, {"ok": True, "rule": jobfeedback.approve(body.get("id") or "")})
                jobfeedback.dismiss(body.get("id") or "")
                return self._send(200, {"ok": True})
            if parts == ["api", "jobs", "unreject"]:
                import jobfeedback
                jobfeedback.unreject(body.get("url") or "")
                return self._send(200, {"ok": True})
            if parts == ["api", "connect", "build"]:
                n = connect.build_queue()
                return self._send(200, {"ok": True, "count": n})
            if parts == ["api", "connect", "find"] or parts == ["api", "connect", "suggest"]:
                # Manual only: 1 empresa/run. Claude roda em background; UI faz poll em finding.
                if connect.FINDING["on"]:
                    return self._send(200, {"ok": True, "started": False, "finding": True})
                threading.Thread(target=connect.suggest, kwargs={"limit": 1}, daemon=True).start()
                return self._send(202, {"ok": True, "started": True})
            if len(parts) == 4 and parts[:2] == ["api", "connect"]:
                connect.act(int(parts[2]), parts[3], body)
                return self._send(200, {"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "engage"]:
                tid, action = int(parts[2]), parts[3]
                if action == "draft":
                    if JOBS["generating"]:
                        return self._send(409, {"error": "Já estou escrevendo, aguarde."})
                    JOBS["generating"] += 1
                    try:
                        return self._send(200, {"ok": True, "data": engage.draft(tid, body.get("post_text"))})
                    finally:
                        JOBS["generating"] -= 1
                if action == "done":
                    engage.mark_done(tid, body.get("comment", ""), body.get("post_url", ""))
                elif action == "skip":
                    engage.skip(tid, body.get("reason", ""))
                elif action == "followup-done":
                    engage.followup_done(tid)
                elif action == "replied":
                    engage.mark_reply(tid)
                else:
                    return self._send(404, {"error": "ação desconhecida"})
                return self._send(200, {"ok": True})
            if parts == ["api", "linkedin", "app"]:
                write_secret(APP_FILE, {"client_id": body["client_id"].strip(), "client_secret": body["client_secret"].strip()})
                return self._send(200, {"ok": True})
            if parts == ["api", "linkedin", "disconnect"]:
                TOKEN_FILE.unlink(missing_ok=True)
                return self._send(200, {"ok": True})
            if parts == ["api", "style"]:
                posts = parse_imported(body.get("text", ""))
                JOBS["generating"] += 1
                try:
                    n = learn_style(posts)
                finally:
                    JOBS["generating"] -= 1
                return self._send(200, {"ok": True, "count": n})
            if len(parts) == 4 and parts[:2] == ["api", "posts"]:
                pid, action = int(parts[2]), parts[3]
                if action == "save":
                    update_post(pid, text=body["text"], first_comment=body.get("first_comment", ""))
                    ensure_link_preview_async(pid)
                elif action == "linkpreview":
                    og = refresh_link_preview(pid)
                    return self._send(200, {"ok": True, "post": get_post(pid), "fetched": og})
                elif action == "approve":
                    return self._send(200, {"ok": True, "result": approve(
                        pid, body.get("text"), body.get("first_comment"), body.get("when", "slot"))})
                elif action == "reject":
                    update_post(pid, status="rejected", feedback=body.get("feedback") or "rejected without comment")
                elif action == "unschedule":
                    update_post(pid, status="pending", scheduled_at=None)
                elif action == "regenerate":
                    threading.Thread(target=regenerate, args=(pid, body.get("feedback", "")), daemon=True).start()
                    return self._send(202, {"ok": True})
                elif action == "popup":
                    threading.Thread(target=approval_popup, args=(pid,), daemon=True).start()
                elif action == "metrics":
                    metrics.record(pid, body.get("impressions"), body.get("reactions"),
                                   body.get("comments"), body.get("shares"))
                elif action == "delete":
                    with db() as con:
                        con.execute("DELETE FROM posts WHERE id=? AND status!='published'", (pid,))
                else:
                    return self._send(404, {"error": "ação desconhecida"})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as e:
            log(traceback.format_exc())
            self._send(500, {"error": str(e)})


def serve():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = cfg()["port"]
    log(f"painel em http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "generate":
        pid = generate(" ".join(sys.argv[2:]) or None, notify=False)
        print(json.dumps(get_post(pid), indent=2, ensure_ascii=False))
    elif cmd == "publish-due":
        publish_due()
    elif cmd == "engage":
        n = engage.build_queue()
        print(f"{n} alvo(s) na fila de hoje\n")
        for t in engage.today_queue():
            who = t["person"] or f"({t['role']})"
            print(f"#{t['id']} [{t['kind']}] {who} — {t['company'] or t['why']}")
            print(f"    posts: {t['activity_url']}")
            if t["job_title"]:
                print(f"    vaga:  {t['job_title']} · {t['job_url']}")
        for f in engage.followups_due():
            print(f"\n⏰ follow-up vencido: #{f['id']} {f['person'] or f['company']} — convite com nota")
    else:
        print(__doc__)
