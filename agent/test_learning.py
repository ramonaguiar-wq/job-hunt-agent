"""Testes da revisão 2026-10-16: aprendizado com rejeições + nota do Quero aplicar.
Roda em HERMES_DIR temporário: não toca nos dados reais.  python3 test_learning.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
(tmp / "data").mkdir()
(tmp / "job-criteria.json").write_text(json.dumps({"rules": {}, "learned": []}))
os.environ["HERMES_DIR"] = str(tmp)
sys.path.insert(0, str(Path(__file__).parent))
import jobfeedback as jf  # noqa: E402

fails = 0
def ok(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond

def rej(company, country, reasons, title="Senior Product Designer", note=""):
    jf._append_feedback({"url": f"https://x/{company}/{len(jf.feedback_rows())}", "title": title, "company": company,
                         "location": f"Remote · {country}", "country": country}, reasons, note)

# 1 rejeição: ainda sem penalidade; 2: -20; 4: teto -30
rej("Acme", "Spain", ["spain"])
p = jf.learn()
ok(not p["country"].get("spain"), "1 rejeição não penaliza")
rej("Beta", "Spain", ["spain"]); p = jf.learn()
ok(p["country"].get("spain") == -20, f"2 rejeições = -20 (veio {p['country'].get('spain')})")
rej("Gama", "Spain", ["spain"]); rej("Delta", "Spain", ["spain"]); p = jf.learn()
ok(p["country"].get("spain") == -30, "teto -30 por item")

# sugestão aparece a partir de 3 repetições
sug = jf.suggestions()
ok(any("spain" in s["text"].lower() for s in sug), "3+ repetições geram sugestão de regra")
rej("Eps", "France", ["remote"]); rej("Zeta", "France", ["remote"])
ok(not any("france" in s["text"].lower() for s in jf.suggestions()), "2 repetições não geram sugestão")

# palavras genéricas não viram penalidade
for i in range(3):
    rej(f"W{i}", "Germany", ["remote"], title="Remote Contract Product Designer")
p = jf.learn()
ok(not {"remote", "contract", "contractor"} & set(p["word"]), f"stopwords fora das palavras ({p['word']})")

# penalidade no job e teto total -60
pts, why = jf.penalty({"company": "Beta", "country": "Spain", "title": "x"}, p)
ok(pts <= -30 and why, f"penalty aplica no job ({pts}, {why})")
big = {"company": {"beta": -30}, "country": {"spain": -30, "x": 0}, "word": {"growth": -30}}
pts, _ = jf.penalty({"company": "Beta", "country": "Spain", "title": "Growth Designer"}, big)
ok(pts == -60, f"teto total -60 (veio {pts})")

# paridade com rank.js
js = (f"const r=require('{Path(__file__).resolve().parent.parent}/monitor/rank.js');"
      f"console.log(JSON.stringify(r.learnedPenalty({{company:'Beta',location:'Remote · Spain',title:'Growth Designer'}},"
      f"{json.dumps(big)})))")
try:
    out = json.loads(subprocess.check_output([os.environ.get("NODE", "node"), "-e", js]))
    ok((out.get("points") if isinstance(out, dict) else out) == -60 or "-60" in json.dumps(out), f"rank.js dá o mesmo -60 ({out})")
except Exception as e:
    ok(False, f"rank.js não rodou: {e}")

# aprovar sugestão grava backup + learned; dispensar some da lista
sug = jf.suggestions()
sid = next(s["id"] for s in sug if "spain" in s["text"].lower())
jf.approve(sid)
crit = json.loads((tmp / "job-criteria.json").read_text())
ok(len(crit.get("learned", [])) == 1, "aprovar adiciona regra em job-criteria.json")
ok(any(f.name.startswith("job-criteria.json.bak-") for f in tmp.iterdir()), "aprovar faz backup antes")

# migração de motivos antigos
ok(jf.LEGACY.get("senior") == "seniority" and jf.LEGACY.get("salary") == "currency", "motivos antigos mapeados")

# nota do Quero aplicar: inglês, <=300, sem inventar
import want  # noqa: E402
n = want.note({"person": "Maximiliana Alexandra de Souza Bragança", "company": "A Very Long Company Name International Holdings",
               "job_title": "Senior Product Designer, Platform & Design Systems (Payments, Risk and Growth)", "role": "Recrutador / Talent"})
ok(len(n) <= 300, f"nota <= 300 car. ({len(n)})")
ok(n.startswith("Hi ") and "Valencia" in n, "nota em inglês com fatos reais")

print(f"\n{'TUDO OK' if not fails else str(fails) + ' FALHA(S)'}")
sys.exit(1 if fails else 0)
