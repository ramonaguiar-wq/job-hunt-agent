// fetch-missing-descriptions.js - Busca a descrição das vagas do pool que o filters.js marca como needs_description (2026-10-16).
// Usa o cache data/description-cache.json; o que falta vem de ats.js (fetchJobDescription) e, no LinkedIn, de fetchLinkedInJobDetail.
// Também refaz vagas com a descrição cortada (2500 chars, o limite do coletor antigo do LinkedIn, ou terminando em reticências) quando o cache não tem versão maior.
// Não remove vagas do pool: quem exclui é o filtrar/filters.js depois.
// Uso: node fetch-missing-descriptions.js [--max N] [--dry] [--no-net]   (--no-net: só conta os candidatos, sem rede nem gravação)
// Última linha do stdout: resumo JSON.
const fs = require('fs'), path = require('path');
const F = require('./filters');
const { scoreJob } = require('./rank');
const { fetchLinkedInJobDetail, Blocked } = require('./linkedin');
const { fetchJobDescription } = require('./ats');

const POOL = path.join(__dirname, 'data', 'engage-jobs.json');
const CACHE = path.join(__dirname, 'data', 'description-cache.json');
const LOG_DIR = path.join(__dirname, 'logs');
const LOG = path.join(LOG_DIR, 'fetch-missing-descriptions.log');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const strip = (h) => String(h || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const isLinkedInJob = (j) => /^linkedin-\d+/.test(j.id || '') || /linkedin\.com\/jobs\/view/.test(j.url || '');
const needsDesc = (j) => (F.assess(j).exclude || {}).key === 'needs_description';
const TRUNC_AT = 2500;
const len = (x) => strip(x).length;
const isTruncated = (j) => len(j.description) === TRUNC_AT || /(…|\.\.\.)$/.test(strip(j.description));
// cortada e sem versão maior no cache (e que ainda não foi tentada de novo): precisa buscar o texto completo
const truncatedNoBigger = (j, e) => isTruncated(j) && !(e && len(e.description) > len(j.description)) && !(e && e.truncRefetched);
// cortada, mas o cache já tem o texto maior: só aplica, sem rede
const truncatedFromCache = (j, e) => isTruncated(j) && e && len(e.description) > len(j.description);

const DRY = process.argv.includes('--dry');
const NO_NET = process.argv.includes('--no-net');
const maxIdx = process.argv.indexOf('--max');
const MAX = maxIdx > 0 ? Number(process.argv[maxIdx + 1]) : Infinity;

fs.mkdirSync(LOG_DIR, { recursive: true });
const log = (msg) => {
  const line = `${new Date().toISOString()} ${msg}`;
  console.error(line);
  try { fs.appendFileSync(LOG, line + '\n'); } catch (_) {}
};
const load = (f, d) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch (_) { return d; } };
const writeAtomic = (file, data) => {
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, file);
};

(async () => {
  const cache = load(CACHE, {});
  const pool0 = load(POOL, { jobs: [] });
  const candidates = (pool0.jobs || []).filter((j) => needsDesc(j) || truncatedNoBigger(j, cache[j.id]) || truncatedFromCache(j, cache[j.id])).slice(0, MAX);
  const nTrunc = candidates.filter((j) => !needsDesc(j)).length;
  const ids = candidates.map(j => j.id);
  if (NO_NET) {
    const semTexto = candidates.filter(needsDesc).length;
    const cortadasCache = candidates.filter((j) => !needsDesc(j) && truncatedFromCache(j, cache[j.id])).length;
    const summary = { candidatos: candidates.length, needs_description: semTexto, cortadas_a_buscar: nTrunc - cortadasCache, cortadas_com_texto_maior_no_cache: cortadasCache };
    console.log(JSON.stringify(summary));
    return;
  }
  log(`início: ${candidates.length} candidatos (${nTrunc} com descrição cortada)${DRY ? ' (dry)' : ''}`);

  const got = new Map(); // id -> { description, location, seniority, closed }
  const falhas = { 403: 0, 404: 0, blocked: 0, outro: 0, sem_texto: 0 };
  let fromCache = 0, fechadas = 0, sinceSave = 0, liBlocked = false, n = 0;
  const saveCache = () => { if (!DRY) writeAtomic(CACHE, { ...load(CACHE, {}), ...cache }); };

  for (const j of candidates) {
    n++;
    let e = cache[j.id];
    if (e && (strip(e.description) || e.closed) && !truncatedNoBigger(j, e)) {
      fromCache++;
    } else {
      const li = isLinkedInJob(j);
      if (li && liBlocked) { falhas.blocked++; continue; }
      try {
        if (li) {
          await sleep(6000 + Math.random() * 6000);
          const d = await fetchLinkedInJobDetail(j);
          e = d ? { ...d, fetchedAt: Date.now() } : null;
          if ((!e || (!e.closed && !strip(e.description)))) {
            // sem texto no detalhe público: tenta ATS/página direta do mesmo job
            const r = await fetchJobDescription(j);
            if (strip(r.description)) e = { ...(e || {}), id: j.id, description: r.description, location: (e && e.location) || r.location || '', fetchedAt: Date.now() };
          }
        } else {
          await sleep(1000);
          const r = await fetchJobDescription(j);
          e = { id: j.id, description: String(r.description || '').trim(), location: r.location || '', closed: false, fetchedAt: Date.now(), url: r.url };
        }
        if (e && isTruncated(j) && len(e.description) <= len(j.description)) e = { ...e, description: e.description || j.description, truncRefetched: true };
        if (e && (strip(e.description) || e.closed)) cache[j.id] = e;
      } catch (err) {
        if (err instanceof Blocked) {
          liBlocked = true; falhas.blocked++;
          log(`LinkedIn bloqueado (${err.message}); sigo só com os outros hosts`);
        } else {
          const m = /HTTP (\d{3})/.exec(err.message);
          if (m && m[1] === '403') falhas[403]++;
          else if (m && (m[1] === '404' || m[1] === '410')) falhas[404]++;
          else falhas.outro++;
          log(`erro ${j.id} ${j.company}: ${err.message}`);
        }
        continue;
      } finally {
        if (++sinceSave >= 10) { sinceSave = 0; saveCache(); }
      }
    }
    if (!e) { falhas.sem_texto++; log(`sem texto ${j.id} ${j.company}`); continue; }
    if (e.closed) fechadas++;
    if (strip(e.description)) got.set(j.id, e);
    else if (e.closed) got.set(j.id, e);
    else { falhas.sem_texto++; log(`sem texto ${j.id} ${j.company}`); }
    log(`${n}/${candidates.length} ${j.id} ${j.company} → ${e.closed ? 'fechada' : strip(e.description).length + ' chars'}`);
  }
  saveCache();

  // Relê o pool do disco (o job-monitor pode ter regravado) e aplica só o que foi obtido, por id.
  const pool = load(POOL, { jobs: [] });
  let obtidos = 0;
  for (let i = 0; i < pool.jobs.length; i++) {
    const e = got.get(pool.jobs[i].id);
    if (!e) continue;
    const j = { ...pool.jobs[i] };
    const text = strip(e.description).slice(0, 8000);
    if (text) { j.description = text; obtidos++; }
    if (e.location && !j.location) j.location = e.location;
    if (e.seniority && !j.seniority) j.seniority = e.seniority;
    if (e.closed && !j.closed) j.closed = true;
    const { score, reasons } = scoreJob(j, strip);
    j.score = score; j.scoreReasons = reasons; j.labels = F.labelsFor(j);
    pool.jobs[i] = j;
  }
  if (!DRY && got.size) writeAtomic(POOL, { ...pool, updatedAt: Date.now() });

  // Situação depois, só dos candidatos (pool relido; em --dry, aplica em memória só pra contar).
  const byId = new Map(pool.jobs.map(j => [j.id, j]));
  const depois = { passam: 0, excluidos: {}, ainda_needs_description: 0 };
  for (const id of ids) {
    const j = byId.get(id);
    if (!j) continue;
    const ex = F.assess(j).exclude;
    if (!ex) depois.passam++;
    else if (ex.key === 'needs_description') depois.ainda_needs_description++;
    else depois.excluidos[ex.key] = (depois.excluidos[ex.key] || 0) + 1;
  }
  const summary = { candidatos: candidates.length, cache: fromCache, obtidos, fechadas, falhas, depois };
  log(`fim: ${JSON.stringify(summary)}`);
  console.log(JSON.stringify(summary));
})().catch((err) => { log(`FALHA: ${err.stack || err.message}`); process.exit(1); });
