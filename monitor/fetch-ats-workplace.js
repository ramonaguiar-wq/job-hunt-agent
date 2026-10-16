// fetch-ats-workplace.js (2026-10-16) - Guarda no pool o modo de trabalho que o ATS declara (Ashby/Lever workplaceType).
// filters.js exclui por ats_hybrid / ats_onsite. Cache: data/ats-workplace-cache.json { [url]: { workplace, fetchedAt } }.
// Também guarda o local declarado pelo ATS (Ashby location, Lever categories.location, Greenhouse location.name) em job.atsLocation.
// Uso: node fetch-ats-workplace.js [--dry] [--refresh]. Throttle ~1 s por requisição; erro de rede = pula e loga.
const fs = require('fs'), path = require('path');
const POOL = path.join(__dirname, 'data', 'engage-jobs.json');
const CACHE = path.join(__dirname, 'data', 'ats-workplace-cache.json');
const DRY = process.argv.includes('--dry'), REFRESH = process.argv.includes('--refresh');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const load = (f, d) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch (_) { return d; } };
const norm = (w) => { w = String(w || '').toLowerCase().replace(/[^a-z]/g, ''); return w === 'onsite' ? 'onsite' : w === 'hybrid' ? 'hybrid' : w === 'remote' ? 'remote' : null; };
const key = (u) => String(u || '').split('?')[0].replace(/\/application\/?$|\/apply\/?$/, '').replace(/\/$/, '');
const getJson = async (u) => { const r = await fetch(u, { signal: AbortSignal.timeout(20000) }); if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); };

(async () => {
  const cache = load(CACHE, {});
  const jobs = load(POOL, { jobs: [] }).jobs || [];
  const boards = Object.create(null), stats = { ashby: 0, lever: 0, cache: 0, erros: 0, novos: 0 };
  for (const j of jobs) {
    const k = key(j.url);
    if (!REFRESH && cache[k] && 'location' in cache[k]) { stats.cache++; continue; }
    let m = k.match(/jobs\.ashbyhq\.com\/([^/]+)\/([0-9a-f-]{36})/);
    if (m) { (boards[m[1]] = boards[m[1]] || []).push([k, m[2]]); continue; }
    m = k.match(/jobs\.(?:eu\.)?lever\.co\/([^/]+)\/([0-9a-f-]{36})/);
    if (m) {
      await sleep(1000);
      try { const p = await getJson(`https://api.lever.co/v0/postings/${m[1]}/${m[2]}`); cache[k] = { workplace: norm(p.workplaceType), raw: p.workplaceType || null, location: (p.categories && (p.categories.location || (p.categories.allLocations || []).join(', '))) || null, fetchedAt: new Date().toISOString() }; stats.lever++; stats.novos++; }
      catch (e) { stats.erros++; console.error(`lever ${k}: ${e.message}`); }
    }
    m = k.match(/greenhouse\.io\/(?:v1\/boards\/)?([^/?#]+)\/jobs\/(\d+)/);
    if (m) {
      await sleep(1000);
      try { const p = await getJson(`https://boards-api.greenhouse.io/v1/boards/${m[1]}/jobs/${m[2]}`); cache[k] = { workplace: null, raw: null, location: (p.location && p.location.name) || null, fetchedAt: new Date().toISOString() }; stats.greenhouse = (stats.greenhouse || 0) + 1; stats.novos++; }
      catch (e) { stats.erros++; console.error(`greenhouse ${k}: ${e.message}`); }
    }
  }
  for (const [board, list] of Object.entries(boards)) {
    await sleep(1000);
    try {
      const d = await getJson(`https://api.ashbyhq.com/posting-api/job-board/${board}`);
      for (const [k, id] of list) {
        const p = (d.jobs || []).find((x) => x.id === id);
        cache[k] = p ? { workplace: norm(p.workplaceType), raw: p.workplaceType || null, location: p.location || null, fetchedAt: new Date().toISOString() } : { workplace: null, raw: null, location: null, gone: true, fetchedAt: new Date().toISOString() };
        stats.ashby++; stats.novos++;
      }
    } catch (e) { stats.erros++; console.error(`ashby ${board}: ${e.message}`); }
  }
  if (DRY) { console.log(JSON.stringify({ dry: true, ...stats })); return; }
  fs.writeFileSync(CACHE, JSON.stringify(cache, null, 2));
  // Relê o pool (o job-monitor pode ter regravado) e grava atômico.
  const pool = load(POOL, null);
  if (!pool || !Array.isArray(pool.jobs)) { console.log(JSON.stringify({ erro: 'pool ilegível', ...stats })); return; }
  let marcados = 0;
  for (const j of pool.jobs) { const e = cache[key(j.url)]; if (e && e.workplace && j.atsWorkplace !== e.workplace) { j.atsWorkplace = e.workplace; marcados++; } if (e && e.location && j.atsLocation !== e.location) { j.atsLocation = e.location; marcados++; } }
  if (marcados) { const tmp = `${POOL}.tmp-${process.pid}`; fs.writeFileSync(tmp, JSON.stringify(pool, null, 2)); fs.renameSync(tmp, POOL); }
  const dist = {}; for (const j of pool.jobs) if (j.atsWorkplace) dist[j.atsWorkplace] = (dist[j.atsWorkplace] || 0) + 1;
  console.log(JSON.stringify({ ...stats, marcados, atsWorkplace: dist }));
})();
