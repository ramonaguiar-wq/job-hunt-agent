#!/usr/bin/env python3
"""O que realmente engajou — e devolver isso pra dentro das próximas gerações.

Sem isto o agente escreve às cegas: ele sabe o que o candidato rejeitou em rascunho,
mas nunca soube o que o LinkedIn premiou depois de publicado. O resultado é que
ele repetia formato ruim com a mesma confiança do formato bom.

Por que os números são digitados à mão: o token do app tem escopo
`openid profile w_member_social` — só escrita. A leitura de métricas exige
`r_member_social`, que o LinkedIn não libera pra app pessoal. Então o caminho é
você digitar 3 números por post (ou colar o export de analytics), e o agente
cuida de transformar isso em instrução de escrita.

Duas fontes de aprendizado:
  - posts   → impressões/reações/comentários por pilar e formato
  - targets → quais comentários renderam resposta do autor (engage.py)
"""
import json
import re
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Abaixo disto a média não significa nada — não vale virar instrução no prompt.
MIN_SAMPLE = 3


def init(con):
    """Colunas de desempenho. Chamado pelo init_db() do agent.py."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(posts)")}
    for c in ("impressions", "reactions", "comments_count", "shares", "metrics_at"):
        if c not in cols:
            con.execute(f"ALTER TABLE posts ADD COLUMN {c} {'TEXT' if c == 'metrics_at' else 'INTEGER'}")
    tcols = {r["name"] for r in con.execute("PRAGMA table_info(targets)")}
    if "got_reply" not in tcols:
        con.execute("ALTER TABLE targets ADD COLUMN got_reply INTEGER DEFAULT 0")


def _db():
    from agent import db
    return db()


def _now():
    from agent import now
    return now()


def _cfg(key, default=None):
    from agent import cfg
    return cfg().get(key, default)


# ---------------------------------------------------------------- entrada
def record(pid, impressions=None, reactions=None, comments=None, shares=None):
    with _db() as con:
        con.execute(
            "UPDATE posts SET impressions=?, reactions=?, comments_count=?, shares=?, metrics_at=?"
            " WHERE id=?",
            (_int(impressions), _int(reactions), _int(comments), _int(shares),
             _now().isoformat(), pid))


def _int(v):
    if v in (None, ""):
        return None
    try:
        return int(str(v).replace(".", "").replace(",", "").strip())
    except ValueError:
        return None


def pending(hours=None):
    """Publicados há tempo suficiente pra terem número, e ainda sem número.

    48h é o padrão porque antes disso o post ainda está sendo distribuído e o
    número engana — um post lento no dia 1 pode ser o melhor da semana no dia 3.
    """
    hours = hours if hours is not None else _cfg("metrics_after_hours", 48)
    cut = (_now() - timedelta(hours=hours)).isoformat()
    with _db() as con:
        rows = con.execute(
            "SELECT id, topic, pillar, format, published_at, substr(text,1,120) AS hook"
            " FROM posts WHERE status='published' AND published_at IS NOT NULL"
            " AND published_at <= ? AND metrics_at IS NULL ORDER BY published_at", (cut,)).fetchall()
    return [dict(r) for r in rows]


def import_csv(text):
    """Cola do export de analytics do LinkedIn. Casa pelo começo do texto do post.

    O export não traz o id do post, então o casamento é por prefixo do texto —
    funciona porque os primeiros ~60 caracteres de um post raramente se repetem.
    """
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        return 0
    with _db() as con:
        posts = [dict(r) for r in con.execute(
            "SELECT id, text FROM posts WHERE status='published'")]
    hit = 0
    for r in rows:
        low = {(k or "").strip().lower(): v for k, v in r.items()}
        body = _first(low, ["post title", "post", "text", "publicação", "título da publicação"])
        if not body:
            continue
        key = _norm(body)[:60]
        match = next((p for p in posts if _norm(p["text"] or "").startswith(key)), None)
        if not match:
            continue
        record(match["id"],
               _first(low, ["impressions", "impressões"]),
               _first(low, ["reactions", "reações", "likes"]),
               _first(low, ["comments", "comentários"]),
               _first(low, ["reposts", "shares", "compartilhamentos"]))
        hit += 1
    return hit


def _first(d, keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ---------------------------------------------------------------- aprendizado
def _rate(p):
    """Engajamento por impressão. Sem impressão, cai pro total bruto de interações."""
    inter = (p.get("reactions") or 0) + (p.get("comments_count") or 0) + (p.get("shares") or 0)
    imp = p.get("impressions") or 0
    return (inter / imp, True) if imp >= 50 else (inter, False)


def scored_posts():
    with _db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, topic, pillar, format, text, impressions, reactions, comments_count,"
            " shares FROM posts WHERE status='published' AND metrics_at IS NOT NULL")]
    for p in rows:
        p["rate"], p["has_impressions"] = _rate(p)
    return sorted(rows, key=lambda p: p["rate"], reverse=True)


def by_dimension(posts, key):
    agg = {}
    for p in posts:
        k = p.get(key) or "?"
        agg.setdefault(k, []).append(p["rate"])
    return {k: (sum(v) / len(v), len(v)) for k, v in agg.items() if len(v) >= MIN_SAMPLE}


def comment_learnings():
    """Comentários que renderam resposta do autor — o sinal de que abriu conversa."""
    with _db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT comment, got_reply FROM targets WHERE status='engaged' AND comment IS NOT NULL")]
    replied = [r for r in rows if r["got_reply"]]
    return {"total": len(rows), "replied": len(replied),
            "examples": [r["comment"] for r in replied[-3:]]}


def summary():
    posts = scored_posts()
    return {
        "measured": len(posts),
        "pending": len(pending()),
        "top": posts[:3],
        "bottom": posts[-2:] if len(posts) > 4 else [],
        "by_pillar": by_dimension(posts, "pillar"),
        "by_format": by_dimension(posts, "format"),
        "comments": comment_learnings(),
    }


def prompt_section():
    """O bloco que entra no build_prompt. Vazio enquanto não houver dado suficiente.

    Silêncio é de propósito: com 1 ou 2 posts medidos, qualquer 'aprendizado' é
    ruído, e instrução ruim é pior que instrução nenhuma.
    """
    posts = scored_posts()
    if len(posts) < MIN_SAMPLE:
        return ""

    def fmt(p):
        unit = f"{p['rate']*100:.1f}% engajamento" if p["has_impressions"] else f"{int(p['rate'])} interações"
        return f"- [{p['pillar']}/{p['format']}] {unit} — {_norm(p['text'])[:150]!r}"

    out = ["# WHAT ACTUALLY PERFORMED (real numbers from the candidate's own posts)",
           "These are measured results, not guesses. Weigh them above your instincts about "
           "what a good LinkedIn post looks like.",
           "## His best performing posts", "\n".join(fmt(p) for p in posts[:3])]
    if len(posts) > 4:
        out += ["## His worst performing posts (avoid what makes these flat)",
                "\n".join(fmt(p) for p in posts[-2:])]

    bp = by_dimension(posts, "pillar")
    bf = by_dimension(posts, "format")
    if bp:
        best = max(bp.items(), key=lambda x: x[1][0])
        worst = min(bp.items(), key=lambda x: x[1][0])
        if best[0] != worst[0]:
            out.append(f"## Pillars\nBest: {best[0]} (n={best[1][1]}). Weakest: {worst[0]} (n={worst[1][1]}). "
                       "Lean toward the strong pillar unless the brief says otherwise.")
    if bf:
        best = max(bf.items(), key=lambda x: x[1][0])
        worst = min(bf.items(), key=lambda x: x[1][0])
        if best[0] != worst[0]:
            out.append(f"## Formats\nBest: {best[0]} (n={best[1][1]}). Weakest: {worst[0]} (n={worst[1][1]}).")

    cl = comment_learnings()
    if cl["examples"]:
        out.append("## Comments of his that earned a reply from the author\n"
                   + "\n".join(f"- {e[:200]!r}" for e in cl["examples"])
                   + "\nThe same instinct applies to posts: what opens a conversation beats what sounds smart.")
    return "\n\n".join(out)


def remind():
    """Cutuca uma vez quando há post publicado esperando número."""
    from agent import notify, log
    try:
        due = pending()
        if not due:
            return
        notify(f"{len(due)} post(s) publicado(s) esperando os números do LinkedIn.",
               "Registre o desempenho")
    except Exception as e:
        log("metrics.remind falhou:", e)
