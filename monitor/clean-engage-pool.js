#!/usr/bin/env node
// clean-engage-pool.js - Reaplica as regras duras (job-criteria.json) ao pool data/engage-jobs.json.
// Revisão 2026-10-16: junta o texto, o local real, a senioridade e o contrato buscados depois
// (data/description-cache.json), decide com o mesmo motor da coleta e do painel (filters.js = criteria.py),
// recalcula "por que combina" e o score, e grava um relatório com o motivo de cada vaga que saiu.
// Vaga sem texto não sai: fica marcada needsDescription. Agência e cargo fora do alvo saem como na coleta (rank.js).
// Uso: node clean-engage-pool.js [--dry]   (--dry não grava o pool; o relatório sai com sufixo -dry)

const fs = require('fs');
const path = require('path');
const F = require('./filters');
const { scoreJob, fitReasons, isAgency, isOffTarget } = require('./rank');

const DATA = path.join(__dirname, 'data');
const FILE = path.join(DATA, 'engage-jobs.json');
const CACHE = path.join(DATA, 'description-cache.json');
const MAX_TEXT = 15000; // igual ao job-monitor
const DRY = process.argv.includes('--dry');
const now = new Date();
const pad = (n) => String(n).padStart(2, '0');
const day = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
const REPORT = path.join(DATA, `pool-report-${day}${DRY ? '-dry' : ''}`);
const RANK = { key: 'rank', label: 'agência ou cargo fora do alvo', ui: 'Empresa/setor' };

const pool = JSON.parse(fs.readFileSync(FILE, 'utf8'));
const cache = fs.existsSync(CACHE) ? JSON.parse(fs.readFileSync(CACHE, 'utf8')) : {};
const before = pool.jobs || [];
const kept = [], rows = [];

for (const old of before) {
  const c = cache[old.id] || {};
  const { remote, ...rest } = old; // campo antigo: as regras não usam
  let j = {
    ...rest,
    description: F.plainText(c.description || old.description || '').slice(0, MAX_TEXT),
    location: c.location || old.location || '',
    seniority: c.seniority || old.seniority || '',
    employmentType: c.employmentType || old.employmentType || '',
    closed: !!(c.closed || old.closed)
  };
  const a = F.assess(j);
  const x = a.exclude || ((isAgency(j) || isOffTarget(j)) ? { ...RANK, evidence: `${j.company} · ${j.title}` } : null);
  const row = {
    id: j.id, title: j.title, company: j.company, location: j.location, source: j.source, url: j.url,
    text: c.description ? 'cache' : (old.description ? 'pool' : 'nenhum'), chars: j.description.length,
    key: x ? x.key : null, label: x ? x.label : null, ui: x ? x.ui : null,
    evidence: x ? String(x.evidence || '').replace(/\s+/g, ' ').slice(0, 200) : null,
    hits: Object.keys(a.hits), remote: a.remote.how || null, scoreBefore: old.score
  };
  if (x && x.key !== 'needs_description') { rows.push({ ...row, decision: 'sai' }); continue; }
  j = { ...j, labels: a.labels, contractor: !!a.contractor.type };
  j.fit = fitReasons(j, F.plainText);
  if (x) j.needsDescription = true; else delete j.needsDescription;
  const { score, reasons } = scoreJob(j, F.plainText);
  j.score = score;
  j.scoreReasons = reasons;
  kept.push(j);
  rows.push({ ...row, decision: x ? 'fica sem texto' : 'fica', scoreAfter: score, labels: a.labels, fit: j.fit });
}
kept.sort((a, b) => (b.score - a.score) || ((b.addedAt || 0) - (a.addedAt || 0)));

const out = rows.filter(r => r.decision === 'sai');
const tally = (list, f) => list.reduce((m, r) => (m[f(r)] = (m[f(r)] || 0) + 1, m), {});
const byLabel = Object.entries(tally(out, r => `${r.label}\t${r.ui}`)).sort((a, b) => b[1] - a[1]);
const noText = rows.filter(r => r.decision === 'fica sem texto').length;
const summary = { at: now.toISOString(), dry: DRY, before: before.length, after: kept.length, noText, removed: out.length,
  removedByKey: tally(out, r => r.key), removedByUi: tally(out, r => r.ui), bySource: tally(rows, r => `${r.source} → ${r.decision}`) };

// ---- Relatório ----
const cell = (s) => String(s == null ? '' : s).replace(/\|/g, '/').replace(/\s+/g, ' ').trim();
const date = `${pad(now.getDate())}/${pad(now.getMonth() + 1)}/${now.getFullYear()} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
const md = [
  `# Pool de vagas: antes e depois (${date}${DRY ? ', simulação' : ''})`,
  '',
  'Regras: `job-criteria.json`, o mesmo motor da coleta (filters.js) e do painel (criteria.py).',
  'Texto de cada vaga: o do pool, trocado pelo do `description-cache.json` quando existe.',
  '',
  '| | Vagas |', '|---|---|',
  `| Antes | ${before.length} |`,
  `| Ficam | ${kept.length - noText} |`,
  `| Ficam sem texto (marcadas pra abrir na mão) | ${noText} |`,
  `| Saem | ${out.length} |`,
  '',
  '## Por que saíram', '',
  '| Motivo | Categoria no painel | Vagas |', '|---|---|---|',
  ...byLabel.map(([k, n]) => { const [label, ui] = k.split('\t'); return `| ${cell(label)} | ${cell(ui)} | ${n} |`; }),
  '',
  '## Ficam (maior score primeiro)', '',
  '| Score | Antes | Vaga | Empresa | Local | Remoto por | Selos | Combina |', '|---|---|---|---|---|---|---|---|',
  ...kept.map(j => {
    const r = rows.find(x => x.id === j.id);
    return `| ${j.score} | ${cell(r.scoreBefore)} | [${cell(j.title)}](${j.url}) | ${cell(j.company)} | ${cell(j.location)} | ${cell(r.remote || (j.needsDescription ? 'sem texto' : ''))} | ${cell(j.labels.join(', '))} | ${cell(j.fit.join(', '))} |`;
  }),
  '',
  '## Saem, por motivo', '',
  ...byLabel.flatMap(([k, n]) => {
    const [label] = k.split('\t');
    return [`### ${label} (${n})`, '',
      ...out.filter(r => r.label === label).map(r => `- [${cell(r.title)}](${r.url}) · ${cell(r.company)} · ${cell(r.location) || 'sem local'} · "${cell(r.evidence).slice(0, 140)}"`), ''];
  })
].join('\n');

fs.writeFileSync(`${REPORT}.json`, JSON.stringify({ ...summary, jobs: rows }, null, 2));
fs.writeFileSync(`${REPORT}.md`, md + '\n');
if (!DRY) fs.writeFileSync(FILE, JSON.stringify({ updatedAt: Date.now(), jobs: kept }, null, 2));

console.log(`Pool: ${before.length} → ${kept.length} (${noText} sem texto, marcadas) · saem ${out.length}${DRY ? ' · simulação, pool não gravado' : ''}`);
for (const [k, n] of byLabel) console.log(`  - ${k.split('\t')[0]} [${k.split('\t')[1]}]: ${n}`);
console.log(`Relatório: ${REPORT}.md e .json`);
