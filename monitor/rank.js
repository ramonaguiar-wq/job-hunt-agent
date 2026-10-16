// rank.js - Limpa o ruído da fila de vagas e ranqueia por fit.
//
// Existe porque a fila crua tinha três problemas: uma mesma empresa (EWOR) inundando
// com 7 vagas quase idênticas, agências/agregadores entrando como se fossem o empregador
// (Jobgether, Hirehire, Eleve Talent...), e match torto de palavra-chave
// ("Mechanical Design Engineer" entrando por "design engineer").
//
// A saída alimenta dois consumidores: o relatório HTML e o engagement queue do
// linkedin-agent (que precisa de empresas reais — agência não tem head of design
// com quem valha a pena conversar).

// Agências de recrutamento, agregadores e consultorias de body shop.
// A vaga pode até ser real, mas quem publica não é o time que você quer alcançar.
const AGENCY = /\b(jobgether|hirehire|eleve ?talent|bright professionals|astek|integer consulting|sinnkontor|ewor|micro1|jobster|recruit|recruiting|recruitment|staffing|talent (group|partners|solutions)|headhunt|consulting group|body ?shop|outsourc)\b/i;

// Consultorias de TI / service providers: contratam em volume pra alocar em cliente.
// O "time de design" não é deles, então não há com quem conversar nem produto pra comentar.
const IT_SERVICES = /\b(hcl ?tech|andersen ?lab|globant|luxoft|epam|capgemini|accenture|infosys|wipro|cognizant|\btcs\b|deloitte|pwc|kpmg|ey |sopra|atos|endava|ciklum|softserve|netcompany|nagarro|thoughtworks)\b/i;

// Cargos que batem na palavra-chave mas não são design de produto digital.
const OFF_TARGET = /\b(mechanical|industrial design engineer|cad|manufacturing|hardware|civil|structural|packaging|textile|fashion|interior|automotive|game (art|artist)|3d artist)\b/i;

// Estágio/bolsa/programa de entrada — a regra de senioridade do job-criteria.json cobre os títulos em inglês,
// mas não pega 'beca'/'becario' (ES) nem 'programa ... trainee'.
const ENTRY_LEVEL = /\b(beca|becari[oa]|pr[áa]cticas|estagi|est[áa]gio|apprentice|graduate (program|scheme)|entry[- ]level|programa step up)\b/i;

// Vaga escrita no idioma local costuma exigir o idioma local.
const LOCAL_LANGUAGE = /\b(m\/w\/d|w\/m\/d|m\/f\/x|h\/f\/x|praca zdalna|zdalna|deutschsprachig|muttersprach|native (german|french|dutch|polish|italian)|fließend|courant)\b/i;

// País único citado como "remote" — o LinkedIn carimba "Remote European Union"
// mesmo quando a vaga está presa a um país que não é a Espanha.
const SINGLE_COUNTRY = /\b(poland|warsaw|hamburg|munich|berlin|athens|greece|finland|helsinki|wroc[lł]aw|budapest|bucharest|prague|lisbon|porto)\b/i;
// Cidades espanholas saíram (2026-10-15): vaga local na Espanha não é alvo; Remote Spain via EOR ganha pelo bônus de remoto.
const HOME_FRIENDLY = /(worldwide|anywhere|global|emea|europe wide|all of europe|united kingdom|\buk\b|ireland|united states|\busa?\b)/i;
const REMOTE_FRIENDLY = /\b(fully remote|remote[- ]first|work from anywhere|worldwide|anywhere|emea|distributed team)\b/i;

function scoreJob(job, stripHtml) {
  const text = `${job.title} ${job.location || ''} ${stripHtml(job.description || '')}`;
  const reasons = [];
  let score = 0;

  // Sinais de fit já calculados pelo job-monitor (AI/LLM, Fintech, AdTech, B2B, DS, Senior)
  score += (job.fit?.length || 0) * 10;

  if (job.ats) { score += 15; reasons.push('aplicação direta'); }
  if (job.contractor) { score += 15; reasons.push('aceita contractor/EOR'); }
  if (REMOTE_FRIENDLY.test(text)) score += 6;
  if (/(€|\beur\b|\beuros?\b|\$|\busd\b)/i.test(`${job.salary || ''} ${text}`)) score += 3;

  // Descrição real vale muito: sem ela não dá pra escrever nada personalizado.
  const desc = stripHtml(job.description || '');
  if (desc.length > 400) score += 8;
  else if (desc.length < 120) { score -= 10; reasons.push('descrição vazia'); }

  if (/\bsenior\b/i.test(job.title)) score += 8;

  if (LOCAL_LANGUAGE.test(text)) { score -= 25; reasons.push('provável exigência de idioma local'); }

  const loc = job.location || '';
  if (HOME_FRIENDLY.test(loc)) score += 12;
  else if (SINGLE_COUNTRY.test(loc)) { score -= 18; reasons.push('remoto preso a um país'); }

  // Frescor: vaga de ontem rende mais que vaga de 6 dias atrás.
  if (job.postedAt) {
    const ageDays = (Date.now() - job.postedAt) / 86400000;
    if (ageDays <= 1) score += 12;
    else if (ageDays <= 3) score += 6;
    else if (ageDays > 5) score -= 6;
  }

  // Aprendido com as rejeições do painel (data/learned-penalties.json, calculado pelo jobfeedback.py).
  const pen = learnedPenalty(job);
  if (pen.points) { score += pen.points; reasons.push(...pen.why); job.learnedPenalty = pen.points; }

  return { score, reasons };
}

// Mesma conta do jobfeedback.penalty() no painel: empresa, país e palavra do título rejeitados 2+ vezes.
const PENALTIES_FILE = require('path').join(__dirname, 'data', 'learned-penalties.json');
let penCache = { mtime: 0, data: {} };
function loadPenalties() {
  try {
    const st = require('fs').statSync(PENALTIES_FILE);
    if (st.mtimeMs !== penCache.mtime) penCache = { mtime: st.mtimeMs, data: JSON.parse(require('fs').readFileSync(PENALTIES_FILE, 'utf8')) };
  } catch (_) { penCache = { mtime: 0, data: {} }; }
  return penCache.data;
}
const PEN_STOP = new Set('de da do das dos e em para com the of and a an for to in at on senior sênior lead head principal product designer design ux ui remote remoto remota contract contractor freelance staff mid level full time part hybrid europe emea worldwide anywhere'.split(' '));
const norm = (s) => String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
function learnedPenalty(job, pens = loadPenalties()) {
  let points = 0; const why = [];
  const c = (pens.company || {})[norm(job.company)];
  if (c) { points += c; why.push(`empresa rejeitada antes (${c})`); }
  const country = job.country || (job.location ? require('./filters').countryOf(job.location) : '');
  const k = country && (pens.country || {})[norm(country)];
  if (k) { points += k; why.push(`país rejeitado antes (${k})`); }
  const words = [...new Set((String(job.title || '').toLowerCase().match(/[a-zà-ú]+/g) || []).filter(w => w.length > 2 && !PEN_STOP.has(w)))];
  const ws = words.map(w => (pens.word || {})[w]).filter(Boolean);
  if (ws.length) { const w = Math.max(-30, ws.reduce((a, b) => a + b, 0)); points += w; why.push(`palavra do título rejeitada antes (${w})`); }
  return { points: Math.max(-60, points), why };
}

// "Por que combina" — sinais do perfil do candidato (AI UX, Retail Media/AdTech, Fintech, B2B SaaS).
// Veio do job-monitor em 2026-10-16 pra o clean-engage-pool recalcular com o texto completo da vaga.
const FIT_SIGNALS = [
  ['AI / LLM', /\b(ai|artificial intelligence|llm|machine learning|genai|agents?)\b/i],
  ['Fintech / pagamentos', /\b(fintech|payments?|banking|card|expense|spend management|lending)\b/i],
  ['Retail Media / AdTech', /\b(retail media|adtech|advertising|ads platform|marketplace)\b/i],
  ['B2B SaaS', /\b(b2b|saas|enterprise|dashboard|platform)\b/i],
  ['Design System', /\bdesign systems?\b/i],
  ['Senior', /\bsenior\b/i]
];

function fitReasons(job, stripHtml) {
  const text = `${job.title} ${stripHtml(job.description || '')}`;
  return FIT_SIGNALS.filter(([, re]) => re.test(text)).map(([label]) => label);
}

function isAgency(job) {
  const c = job.company || '';
  return AGENCY.test(c) || AGENCY.test(job.source || '') || IT_SERVICES.test(c);
}

function isOffTarget(job) {
  const t = job.title || '';
  return OFF_TARGET.test(t) || ENTRY_LEVEL.test(t);
}

// Ordena por score, descarta agência/off-target, e limita quantas vagas cada
// empresa pode ocupar na fila (senão uma só empresa toma todas as vagas do dia).
function rankJobs(jobs, stripHtml, { limit = 5, perCompany = 1 } = {}) {
  const scored = jobs
    .filter(j => !isOffTarget(j))
    .filter(j => !isAgency(j))
    .map(j => {
      const { score, reasons } = scoreJob(j, stripHtml);
      return { ...j, score, scoreReasons: reasons };
    })
    .sort((a, b) => b.score - a.score);

  const perCompanyCount = new Map();
  const out = [];
  for (const j of scored) {
    const key = (j.company || '').toLowerCase().trim();
    const n = perCompanyCount.get(key) || 0;
    if (n >= perCompany) continue;
    perCompanyCount.set(key, n + 1);
    out.push(j);
    if (out.length >= limit) break;
  }
  return out;
}

module.exports = { rankJobs, isAgency, isOffTarget, scoreJob, fitReasons, learnedPenalty };
