// linkedin.js - Vagas do LinkedIn pela página pública (sem login).
// Nunca usa a conta do candidato: nada de cookies/sessão, então não há risco de bloqueio da conta.
// No pior caso o LinkedIn barra o IP temporariamente (HTTP 429/999) e a fonte fica vazia naquela rodada.
//
// Revisão 2026-10-16:
//  - O local agora é o do anúncio ("Budapest, Hungary"), sem o prefixo "Remote <região> ·" que o coletor
//    inventava. A região da busca vai em `geo` e a tag remota do LinkedIn em `remoteTag` (não prova nada:
//    quem decide se é remoto é o texto, ver job-criteria.json).
//  - 429/999: backoff exponencial (30s, 60s, 120s) antes de desistir da rodada, e pausa de 2h depois disso.
//  - Rodízio de buscas: 2 buscas por rodada × 5 regiões × no máximo 2 páginas (ordenadas por data).
//  - Log legível em agent/data/agent.log (o mesmo que o painel mostra).

const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search';
const DETAIL = 'https://www.linkedin.com/jobs-guest/jobs/api/jobPosting';
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';

// Rodízio: cada rodada pega as próximas QUERIES_PER_RUN buscas da lista (estado em data/linkedin-state.json).
const QUERIES = [
  'senior product designer',
  'product designer contract',
  'product designer b2b',
  'design systems designer',
  'ai product designer'
];
const QUERIES_PER_RUN = 2;
// Remoto (f_WT=2) — geoIds conferidos em 2026-10-15 pelo endpoint guest.
// Espanha fica de fora: vaga local espanhola não é alvo; Remote Spain de empresa estrangeira vem pela EU/EMEA.
const GEOS = [
  { name: 'European Union', geoId: '91000000' },
  { name: 'EMEA', geoId: '91000007' },
  { name: 'United Kingdom', geoId: '101165590' },
  { name: 'Ireland', geoId: '104738515' },
  { name: 'United States', geoId: '103644278' }
];
const MAX_PAGES = 2;           // 25 vagas por página, mais novas primeiro
const MAX_WINDOW_DAYS = 3;     // a rodada é de hora em hora; 3 dias cobre o rodízio com folga
const BACKOFF_MS = [30000, 60000, 120000];
const COOLDOWN_MS = 2 * 3600 * 1000; // depois de esgotar o backoff, deixa o LinkedIn em paz por 2h

const STATE_FILE = path.join(__dirname, 'data', 'linkedin-state.json');
const AGENT_LOG = process.env.LINKEDIN_AGENT_LOG || path.join(__dirname, '..', 'agent', 'data', 'agent.log');

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const jitter = () => 4000 + Math.random() * 5000; // 4–9s entre requests

const stamp = () => {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};
function log(msg) {
  const line = `[${stamp()}] job-monitor/linkedin: ${msg}`;
  console.error(line);
  try { fs.appendFileSync(AGENT_LOG, line + '\n'); } catch (_) { /* log é best-effort */ }
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch (_) { return { next: 0, blockedUntil: 0 }; }
}
function saveState(s) {
  try { fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true }); fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2)); } catch (_) {}
}

// O LinkedIn às vezes escapa duas vezes ("&amp;amp;"): decodifica até estabilizar.
const decode1 = (s) => s.replace(/&#39;|&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const decode = (s) => {
  let t = String(s || '').replace(/<[^>]+>/g, '');
  for (let i = 0; i < 3; i++) { const n = decode1(t); if (n === t) break; t = n; }
  return t.replace(/\s+/g, ' ').trim();
};

// Contadores da rodada (o job-monitor grava em collect-status.json e o painel mostra)
const stats = { requests: 0, http429: 0, retries: 0, blocked: false, skipped: '', errors: [], queries: [], found: 0, details: 0 };
function resetStats() {
  Object.assign(stats, { requests: 0, http429: 0, retries: 0, blocked: false, skipped: '', errors: [], queries: [], found: 0, details: 0 });
}

class Blocked extends Error {}

// GET com backoff exponencial em 429/999. Depois de esgotar as tentativas, marca bloqueio (cooldown de 2h).
async function getWithBackoff(url, what) {
  const state = loadState();
  if (state.blockedUntil && Date.now() < state.blockedUntil) {
    throw new Blocked(`em pausa até ${new Date(state.blockedUntil).toLocaleTimeString('pt-BR')} (bloqueio anterior)`);
  }
  for (let attempt = 0; ; attempt++) {
    stats.requests++;
    const res = await fetch(url, {
      headers: { 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9' },
      signal: AbortSignal.timeout(20000)
    });
    if (res.status !== 429 && res.status !== 999) return res;
    stats.http429++;
    if (attempt >= BACKOFF_MS.length) {
      stats.blocked = true;
      saveState({ ...loadState(), blockedUntil: Date.now() + COOLDOWN_MS });
      log(`HTTP ${res.status} em ${what} — desisti depois de ${attempt} tentativas; LinkedIn em pausa por 2h`);
      throw new Blocked(`HTTP ${res.status} depois de ${attempt} tentativas`);
    }
    const wait = BACKOFF_MS[attempt] + Math.random() * 5000;
    stats.retries++;
    log(`HTTP ${res.status} em ${what} — esperando ${Math.round(wait / 1000)}s (tentativa ${attempt + 1}/${BACKOFF_MS.length})`);
    await sleep(wait);
  }
}

function parseCards(html, geo) {
  return html.split('<li>').slice(1).map(card => {
    const id = card.match(/urn:li:jobPosting:(\d+)/)?.[1];
    if (!id) return null;
    const date = card.match(/<time[^>]*datetime="([^"]+)"/)?.[1];
    return {
      id: `linkedin-${id}`,
      title: decode(card.match(/base-search-card__title">([\s\S]*?)<\/h3>/)?.[1]),
      company: decode(card.match(/base-search-card__subtitle">([\s\S]*?)<\/h4>/)?.[1]),
      url: `https://www.linkedin.com/jobs/view/${id}/`,
      location: decode(card.match(/job-search-card__location">([\s\S]*?)<\/span>/)?.[1]), // local do anúncio, sem enfeite
      geo,              // região da busca (EU, EMEA, UK...)
      remoteTag: true,  // o LinkedIn marcou como remoto (f_WT=2) — não é prova, o texto decide
      remote: true,     // compatibilidade com código antigo; filters.js ignora
      description: '',
      postedAt: date ? Date.parse(date) : null,
      source: `LinkedIn (${geo})`
    };
  }).filter(Boolean);
}

// Busca da rodada. Devolve as vagas achadas; `stats` diz o que aconteceu (429, bloqueio, erros).
async function fetchLinkedInJobs(maxAgeDays = MAX_WINDOW_DAYS) {
  resetStats();
  const state = loadState();
  if (state.blockedUntil && Date.now() < state.blockedUntil) {
    stats.skipped = `em pausa até ${new Date(state.blockedUntil).toLocaleString('pt-BR')} (429 na rodada anterior)`;
    log(`busca pulada: ${stats.skipped}`);
    return [];
  }
  const start = (state.next || 0) % QUERIES.length;
  const queries = Array.from({ length: Math.min(QUERIES_PER_RUN, QUERIES.length) }, (_, i) => QUERIES[(start + i) % QUERIES.length]);
  saveState({ ...state, next: (start + queries.length) % QUERIES.length });
  stats.queries = queries;
  const windowSecs = Math.min(maxAgeDays, MAX_WINDOW_DAYS) * 86400;
  log(`coleta: ${queries.map(q => `"${q}"`).join(' + ')} × ${GEOS.length} regiões, até ${MAX_PAGES} páginas`);

  const jobs = [];
  for (const q of queries) {
    for (const geo of GEOS) {
      let got = 0;
      for (let page = 0; page < MAX_PAGES; page++) {
        const params = new URLSearchParams({
          keywords: q, geoId: geo.geoId, f_WT: '2', // remoto (segundo o LinkedIn)
          f_TPR: `r${windowSecs}`, sortBy: 'DD', start: String(page * 25)
        });
        await sleep(jitter());
        try {
          const res = await getWithBackoff(`${BASE}?${params}`, `busca "${q}" ${geo.name} p${page + 1}`);
          if (!res.ok) { stats.errors.push(`HTTP ${res.status} (${q} · ${geo.name})`); log(`HTTP ${res.status} em "${q}" ${geo.name}`); break; }
          const batch = parseCards(await res.text(), geo.name);
          jobs.push(...batch);
          got += batch.length;
          if (batch.length < 10) break; // acabaram os resultados
        } catch (err) {
          if (err instanceof Blocked) { stats.found = jobs.length; log(`coleta interrompida: ${err.message}; ${jobs.length} vagas guardadas`); return jobs; }
          stats.errors.push(`${err.message} (${q} · ${geo.name})`);
          log(`erro em "${q}" ${geo.name}: ${err.message}`);
          break;
        }
      }
      log(`"${q}" · ${geo.name}: ${got} vagas`);
    }
  }
  stats.found = jobs.length;
  log(`coleta ok: ${jobs.length} vagas em ${stats.requests} requests (${stats.http429} × 429)`);
  return jobs;
}

const htmlText = (s) => String(s || '').replace(/<(br|\/p|\/li|\/h\d)\s*\/?>/gi, '\n').replace(/<[^>]+>/g, ' ')
  .replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&nbsp;/g, ' ').replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, '\n').trim();

// Página pública da vaga (sem login): descrição + local exato + senioridade + tipo de contrato.
// O LinkedIn não diz aqui se é remoto/híbrido; isso continua vindo do texto.
async function fetchLinkedInJobDetail(jobOrId) {
  const raw = typeof jobOrId === 'object' ? (jobOrId.id || jobOrId.url) : jobOrId;
  const id = String(raw || '').match(/(\d{6,})/)?.[1];
  if (!id) return null;
  const res = await getWithBackoff(`${DETAIL}/${id}`, `detalhe ${id}`);
  stats.details++;
  if (res.status === 404 || res.status === 410) return { id, closed: true, description: '' };
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const html = await res.text();
  const criteria = {};
  for (const m of html.matchAll(/description__job-criteria-subheader">([\s\S]*?)<\/h3>\s*<span[^>]*>([\s\S]*?)<\/span>/g)) {
    criteria[decode(m[1]).toLowerCase()] = decode(m[2]);
  }
  const flavors = [...html.matchAll(/topcard__flavor topcard__flavor--bullet">([\s\S]*?)<\/span>/g)].map(m => decode(m[1])).filter(Boolean);
  return {
    id,
    description: htmlText(html.match(/show-more-less-html__markup[^>]*>([\s\S]*?)<\/div>/)?.[1] || ''),
    location: flavors[0] || '',
    seniority: criteria['seniority level'] || '',
    employmentType: criteria['employment type'] || '',
    closed: /no longer accepting applications/i.test(html)
  };
}

// Compatibilidade (ats.js): só o texto.
async function fetchLinkedInJobDescription(jobOrId) {
  const d = await fetchLinkedInJobDetail(jobOrId);
  return d ? d.description : '';
}

module.exports = { fetchLinkedInJobs, fetchLinkedInJobDetail, fetchLinkedInJobDescription, linkedInStats: stats, log, QUERIES, GEOS, Blocked };
