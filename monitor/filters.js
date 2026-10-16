'use strict';
// filters.js - Motor das regras duras de vaga (revisão 2026-10-16).
// As regras moram em job-criteria.json; aqui elas só são aplicadas. Espelho em Python: agent/criteria.py.
// Mudou uma regra? Edite o JSON e rode `node test-criteria.js`: cada caso roda em JS e em Python e os dois têm de bater.
//   assess(job)      → { exclude, remote, contractor, currency, country, lang, labels, hits }
//   hardExclude(job) → { key, label, ui, evidence } da primeira regra (na ordem de "order") que exclui, ou null
// Paridade: todo texto passa por normalize() (minúsculas, só ' ' e '\n' como espaço, nada fora do BMP) e todo padrão
// do JSON compila sem flag de caixa ('g' aqui, re.A lá), então \b, \w, \s, '.' e os índices batem nos dois.

const fs = require('fs');
const path = require('path');

const RULES_FILE = process.env.JOB_CRITERIA || path.join(__dirname, 'job-criteria.json');

// ---- Normalização (idêntica a criteria.normalize) ----
const asText = (v) => (v === null || v === undefined || v === false ? '' : Array.isArray(v) ? v.map(asText).join(', ') : String(v));
const ENTITIES = { nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", rsquo: "'", lsquo: "'", ldquo: '"', rdquo: '"', ndash: '–', mdash: '—', hellip: '…', bull: '•', middot: '·', euro: '€', pound: '£' };
const BLOCK_TAG = /<\/?(br|p|li|ul|ol|div|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>/gi;
const ANY_TAG = /<[^>]+>/g;
const ENTITY = /&(#\d+|#x[0-9a-f]+|[a-z]+);/gi;

function decodeEntity(whole, body) {
  const b = body.toLowerCase();
  if (b[0] === '#') {
    const n = b[1] === 'x' ? parseInt(b.slice(2), 16) : parseInt(b.slice(1), 10);
    return n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : whole;
  }
  return Object.prototype.hasOwnProperty.call(ENTITIES, b) ? ENTITIES[b] : whole;
}

// HTML vira texto (duas passadas: há descrição com HTML escapado duas vezes), emoji e afins viram espaço,
// espaços exóticos viram ' ', aspas curvas viram retas, e tudo fica em minúsculas.
function normalize(s) {
  let t = asText(s);
  for (let k = 0; k < 2; k++) t = t.replace(BLOCK_TAG, '\n').replace(ANY_TAG, ' ').replace(ENTITY, decodeEntity);
  t = t.replace(/[\ud800-\udbff][\udc00-\udfff]/g, ' ').replace(/[\ud800-\udfff]/g, ' ').normalize('NFC');
  return t.replace(/[\u200b\u200c\u200d\u2060\u00ad]/g, '')
    .replace(/[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]/g, ' ')
    .replace(/\r\n?/g, '\n')
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g, ' ')
    .replace(/[\u2018\u2019\u02bc]/g, "'").replace(/[\u201c\u201d]/g, '"').replace(/[\u2010\u2011]/g, '-')
    .replace(/[ \t]+/g, ' ').replace(/ ?\n[ \n]*/g, '\n')
    .replace(/^[ \n]+|[ \n]+$/g, '')
    .toLowerCase();
}

// Texto legível pra guardar no pool: o mesmo HTML→texto do normalize, sem minúsculas nem troca de caracteres.
// A coleta decide sobre o texto já convertido e guarda esse mesmo texto, então o painel decide igual.
function plainText(s) {
  let t = asText(s);
  for (let k = 0; k < 2; k++) t = t.replace(BLOCK_TAG, '\n').replace(ANY_TAG, ' ').replace(ENTITY, decodeEntity);
  return t.replace(/\r\n?/g, '\n').replace(/[ \t]+/g, ' ').replace(/ ?\n[ \n]*/g, '\n').replace(/^[ \n]+|[ \n]+$/g, '');
}

// ---- Compilação do JSON ----
function compile(raw) {
  const E = raw.engine, R = raw.rules, L = R.language, C = R.currency, T = R.contractor, O = R.onsite;
  const sources = [];
  const expand = (s) => (s === '@spain' ? R.spain_local.places : s.split('{LANGS}').join(L.requiredLanguages).split('{AMT}').join(E.amount));
  const rx = (s) => { const src = expand(s); sources.push(src); return new RegExp(src, 'g'); };
  const pat = (p) => (typeof p === 'string' ? { re: rx(p), unless: [], exceptBefore: [], exceptAfter: [] }
    : { re: rx(p.re), unless: (p.unless || []).map(rx), exceptBefore: (p.exceptBefore || []).map(rx), exceptAfter: (p.exceptAfter || []).map(rx) });
  const group = (patterns, o = {}) => ({
    pats: patterns.map(pat), exceptBefore: (o.exceptBefore || []).map(rx), exceptAfter: (o.exceptAfter || []).map(rx),
    afterChecks: (o.afterChecks || []).map(([r, w]) => [rx(r), w]), noNeg: !!o.noNeg,
  });
  const guards = (r) => ({ exceptBefore: r.exceptBefore, exceptAfter: r.exceptAfter });
  return {
    raw, sources, order: raw.order, R,
    win: { neg: E.negationWindow, guard: E.guardWindow, after: E.afterWindow, ctx: E.evidenceContext },
    minDesc: E.minDescriptionChars, minTokens: E.minLangTokens, minHits: E.minLangHits,
    clauseSplit: rx(E.clauseSplit), guardSplit: rx(E.guardSplit), sentenceSplit: rx(E.sentenceSplit), token: rx(E.tokenPattern),
    negBefore: rx(E.negationBefore), negIgnore: rx(E.negationIgnore), negAfter: rx(E.negationAfter), synthetic: rx(E.syntheticLocationPrefix),
    seniority: group(R.seniority.patterns), seniorityMeta: group(R.seniority.metaPatterns),
    learned: (raw.learned || []).map((l) => ({ id: asText(l.id), field: l.field, label: asText(l.label || l.id), ui: l.ui || R.learned.ui, g: group([l.re], { noNeg: l.field !== 'text' }) })),
    countryHead: group(R.country.headPatterns), countryHeadUnless: rx(R.country.headUnless), countryText: group(R.country.textPatterns),
    candSources: R.country.candidateLocation.sources, candAllowed: rx(R.country.candidateLocation.allowed),
    onsiteExplicit: group(O.explicit, { exceptBefore: O.explicitExceptBefore, exceptAfter: O.explicitExceptAfter }),
    onsiteLabels: group(O.labels, { exceptBefore: O.labelsExceptBefore, exceptAfter: O.labelsExceptAfter }),
    relocation: group(R.relocation.patterns, { afterChecks: [[R.relocation.placeOk, E.placeWindow]] }),
    authorization: group(R.authorization.patterns, { exceptAfter: R.authorization.exceptAfter }),
    spainPlaces: rx(R.spain_local.places), spainHead: R.spain_local.headPatterns.map(rx), spainForeign: group(R.spain_local.foreignOk),
    remoteOption: group(R.remoteOption.patterns, guards(R.remoteOption)), remoteCulture: group(R.remoteCulture.patterns, guards(R.remoteCulture)),
    remoteConfirm: group(R.remoteConfirm.patterns, guards(R.remoteConfirm)), remoteHead: group(R.remoteConfirm.headPatterns, guards(R.remoteConfirm)),
    remoteFriendly: rx(R.no_remote.friendly || '(?!)'),
    remoteGenericEv: rx(R.no_remote.genericEvidence || '(?!)'), remoteGenericCtx: rx(R.no_remote.genericContext || '(?!)'),
    seniorityConfirm: rx(R.seniority.confirm || '(?!)'),
    atsLocRemote: rx(R.no_remote.atsLocationRemote || '(?!)'), atsLocNot: rx(R.no_remote.atsLocationNot || '(?!)'),
    spanishBase: rx(R.spain_local.spanishBase || '(?!)'), foreignEntity: rx(R.spain_local.foreignEntity || '(?!)'),
    langs: Object.keys(L.stopwords), stop: Object.fromEntries(Object.entries(L.stopwords).map(([k, w]) => [k, new Set(w.map(normalize))])),
    langAllowed: L.allowed, langWhen: Object.fromEntries(Object.entries(L.allowedWhenPlace || {}).map(([k, r]) => [k, rx(r)])),
    langRequire: group(L.requirePatterns, { exceptAfter: L.requireExceptAfter }), langOptional: rx(L.optional), langLocal: rx(L.localTokens),
    currencyGood: group(C.good, { noNeg: true }), currencyBad: group(C.bad, { noNeg: true }), currencyCodes: C.codes.map(([k, r]) => [k, rx(r)]),
    contractorTypes: T.types.map(([k, r]) => [k, group([r])]), contractorMeta: rx(T.metaContract),
    countries: raw.countries.map(([k, r]) => [k, rx(r)]),
  };
}

// Recarrega quando o JSON muda (mtime/tamanho). JSON quebrado no meio de uma edição: segue com a última versão boa.
let cache = null;
function criteria() {
  const st = fs.statSync(RULES_FILE);
  const key = `${st.mtimeMs}:${st.size}`;
  if (cache && cache.key === key) return cache.c;
  try {
    cache = { key, c: compile(JSON.parse(fs.readFileSync(RULES_FILE, 'utf8'))) };
  } catch (e) {
    if (!cache) throw e;
    console.error(`filters.js: ${RULES_FILE} inválido, mantendo a versão anterior: ${e.message}`);
    cache.key = key;
  }
  return cache.c;
}

// ---- Motor ----
const WORD = /[A-Za-z0-9_]/;
const has = (re, s) => { re.lastIndex = 0; return re.test(s); };
const lastPiece = (s, splitter) => { const parts = s.split(splitter); return parts[parts.length - 1]; };
// Janelas não cortam palavra ao meio; senão um \b na borda da janela daria resultado diferente do texto inteiro.
function cutStart(text, s, limit) { s = Math.max(0, s); while (s > 0 && s < limit && WORD.test(text[s - 1]) && WORD.test(text[s])) s++; return s; }
function cutEnd(text, e, start) { e = Math.min(text.length, e); while (e > start && e < text.length && WORD.test(text[e - 1]) && WORD.test(text[e])) e++; return e; }

// "no relocation", "not hybrid", "relocation is not required"
function negated(c, text, i, end) {
  const before = lastPiece(text.slice(cutStart(text, i - c.win.neg, i), i), c.clauseSplit).replace(c.negIgnore, ' ');
  return has(c.negBefore, before) || has(c.negAfter, text.slice(end, cutEnd(text, end + c.win.after, end)));
}

function guarded(c, text, i, end, p, g) {
  const before = lastPiece(text.slice(cutStart(text, i - c.win.guard, i), i), c.guardSplit);
  const after = text.slice(end, cutEnd(text, end + c.win.after, end));
  if (p.exceptBefore.some((r) => has(r, before)) || g.exceptBefore.some((r) => has(r, before))) return true;
  if (p.exceptAfter.some((r) => has(r, after)) || g.exceptAfter.some((r) => has(r, after))) return true;
  return g.afterChecks.some(([r, w]) => has(r, text.slice(end, cutEnd(text, end + w, end))));
}

function evidence(c, text, i, end) {
  const s = cutStart(text, i - c.win.ctx, i), e = cutEnd(text, end + c.win.ctx, end);
  const ev = text.slice(s, e).split('\n').join(' / ').replace(/^ +| +$/g, '');
  return (s > 0 ? '…' : '') + ev + (e < text.length ? '…' : '');
}

// Primeiro achado válido do grupo: o de menor índice no texto (empate: o padrão listado antes). Achado negado ou
// barrado por guarda (exceptBefore, exceptAfter, afterChecks) não conta, e a busca segue do caractere seguinte.
function firstHit(c, text, g) {
  if (!text) return null;
  let best = null;
  for (const p of g.pats) {
    if (p.unless.some((u) => has(u, text))) continue;
    let pos = 0;
    for (;;) {
      p.re.lastIndex = pos;
      const m = p.re.exec(text);
      if (!m || (best && m.index >= best.index)) break;
      const i = m.index, end = i + m[0].length;
      if ((g.noNeg || !negated(c, text, i, end)) && !guarded(c, text, i, end, p, g)) { best = { index: i, end, match: m[0] }; break; }
      pos = i + 1;
    }
  }
  if (best) best.evidence = evidence(c, text, best.index, best.end);
  return best;
}

// O prefixo "Remote <região> · " que o coletor antigo punha no local não é do anúncio: sai antes de qualquer regra.
function views(c, job) {
  const title = normalize(job.title);
  const loc = normalize(job.location).replace(c.synthetic, '');
  const head = `${title}\n${loc}`;
  const body = normalize(job.description);
  return { title, loc, head, body, full: `${head}\n${body}` };
}

function remoteOf(c, v) {
  const opt = firstHit(c, v.full, c.remoteOption), cult = firstHit(c, v.full, c.remoteCulture);
  const conf = firstHit(c, v.full, c.remoteConfirm), headConf = firstHit(c, v.head, c.remoteHead);
  const [how, h] = opt ? ['opção remota', opt] : cult ? ['cultura remota', cult] : conf ? [conf.index < v.head.length ? 'título/local' : 'texto', conf] : headConf ? ['título/local', headConf] : ['', null];
  return { opt, cult, confirmed: !!h, how, evidence: h ? h.evidence : '' };
}

function langOf(c, body) {
  const tokens = body.match(c.token) || [];
  if (tokens.length < c.minTokens) return { code: null, n: 0, en: 0 };
  let code = null, n = 0, en = 0;
  for (const k of c.langs) {
    const stop = c.stop[k];
    let m = 0;
    for (const t of tokens) if (stop.has(t)) m++;
    if (k === 'en') en = m;
    if (m > n) { code = k; n = m; }
  }
  return { code, n, en };
}

function languageOf(c, v) {
  const L = c.R.language, lg = langOf(c, v.body), when = lg.code ? c.langWhen[lg.code] : null;
  if (lg.code && !c.langAllowed.includes(lg.code) && lg.n >= c.minHits && lg.n > lg.en && !(when && has(when, v.head)))
    return { code: lg.code, evidence: `anúncio em ${L.names[lg.code] || lg.code} (${lg.code} ${lg.n} × en ${lg.en})` };
  for (let s of `${v.title}\n${v.body}`.split(c.sentenceSplit)) {
    s = s.replace(/^ +| +$/g, '');
    if (!s || has(c.langOptional, s)) continue;
    if (firstHit(c, s, c.langRequire) || has(c.langLocal, s)) return { code: lg.code, evidence: s.length > 200 ? `${s.slice(0, 200)}…` : s };
  }
  return { code: lg.code, evidence: null };
}

function currencyOf(c, job) {
  const text = normalize(`${asText(job.salary)}\n${asText(job.description)}`);
  const good = firstHit(c, text, c.currencyGood), bad = firstHit(c, text, c.currencyBad);
  const codes = c.currencyCodes.filter(([, r]) => has(r, text)).map(([k]) => k);
  const status = bad && !good ? 'bad' : good || codes.length ? 'ok' : 'unknown';
  return { status, codes, evidence: status === 'bad' ? bad.evidence : good ? good.evidence : '' };
}

function contractorOf(c, job, v) {
  const text = `${normalize(job.tags)}\n${v.head}\n${v.body}`;
  for (const [type, g] of c.contractorTypes) { const h = firstHit(c, text, g); if (h) return { type, evidence: h.evidence }; }
  const meta = normalize(job.employmentType);
  if (meta && has(c.contractorMeta, meta)) return { type: 'Contractor', evidence: `tipo no LinkedIn: ${meta}` };
  return { type: null, evidence: '' };
}

function countryHit(c, job, v) {
  // Achado no título: só o próprio título anula. Achado no local: título ou local anulam.
  const ht = firstHit(c, v.title, c.countryHead);
  if (ht && !has(c.countryHeadUnless, v.title)) return ht.evidence;
  const hl = ht ? null : firstHit(c, v.loc, c.countryHead);
  if (hl && !has(c.countryHeadUnless, v.head)) return hl.evidence;
  const t = firstHit(c, v.full, c.countryText);
  if (t) return t.evidence;
  const src = normalize(job.source);
  if (v.loc && c.candSources.some((s) => src.includes(s)) && !has(c.candAllowed, v.loc)) return `onde o candidato pode morar: ${v.loc}`;
  return null;
}

// País do local: o que aparece por último ("Valencia, Valencian Community, Spain" → Spain); empate fica com o da lista.
function countryIn(c, loc) {
  let best = null, at = -1;
  for (const [name, re] of c.countries) {
    let pos = 0, last = -1;
    for (;;) { re.lastIndex = pos; const m = re.exec(loc); if (!m) break; last = m.index; pos = m.index + Math.max(1, m[0].length); }
    if (last > at) { at = last; best = name; }
  }
  return best;
}

// Todas as regras rodam (hits mostra tudo o que disparou); a exclusão é a primeira de "order" que disparou.
// Tirar uma chave de "order" desliga a exclusão por ela, mas o achado continua em hits.
function assess(job) {
  const c = criteria(), R = c.R;
  job = job || {};
  const v = views(c, job);
  let remote = remoteOf(c, v);
  // Vaga do LinkedIn: 'Remote' só no título/local não conta; precisa de frase remota no corpo.
  const fromLinkedIn = /^linkedin-/.test(String(job.id || '')) || /linkedin/i.test(String(job.source || ''));
  if (fromLinkedIn && R.no_remote.linkedinBodyOnly && remote.confirmed) {
    const rb = remoteOf(c, { ...v, head: '', full: v.body });
    if (!rb.confirmed) remote = rb;
  }
  // Local/modo declarado pelo próprio ATS conta como frase remota (não anula ats_hybrid/ats_onsite, país, spain_local).
  if (!remote.confirmed) {
    const atsLoc = normalize(job.atsLocation), atsWp = String(job.atsWorkplace || '').toLowerCase();
    const locOk = atsLoc && has(c.atsLocRemote, atsLoc) && !has(c.atsLocNot, atsLoc);
    if ((locOk || atsWp === 'remote') && atsWp !== 'hybrid' && atsWp !== 'onsite') remote = { opt: null, cult: null, confirmed: true, how: 'ATS', evidence: atsLoc || 'workplaceType remote' };
  }
  const hits = {};
  let learnedHit = null;

  if (job.closed === true) hits.closed = 'a vaga não aceita mais candidaturas';

  const sen = firstHit(c, v.title, c.seniority);
  if (sen) hits.seniority = sen.evidence;
  else {
    const meta = normalize(job.seniority);
    if (meta && firstHit(c, meta, c.seniorityMeta)) hits.seniority = `nível no LinkedIn: ${meta}`;
  }

  for (const l of c.learned) {
    const text = l.field === 'text' ? v.full : l.field === 'title' ? v.title : l.field === 'location' ? v.loc : normalize(job.company);
    const h = firstHit(c, text, l.g);
    if (h) { hits.learned = h.evidence; learnedHit = l; break; }
  }

  const atsW = String(job.atsWorkplace || '').toLowerCase();
  if (atsW === 'onsite') hits.ats_onsite = 'ATS (workplaceType) declara presencial';
  if (atsW === 'hybrid') hits.ats_hybrid = 'ATS (workplaceType) declara híbrido';

  const country = countryHit(c, job, v);
  if (country) hits.country = country;

  const explicit = firstHit(c, v.full, c.onsiteExplicit), label = firstHit(c, v.full, c.onsiteLabels);
  if (explicit && !remote.opt) hits.onsite = explicit.evidence;
  else if (label && !remote.opt && !remote.cult) hits.onsite = label.evidence;

  const reloc = firstHit(c, v.full, c.relocation);
  if (reloc) hits.relocation = reloc.evidence;
  const auth = firstHit(c, v.full, c.authorization);
  if (auth) hits.authorization = auth.evidence;

  if (!v.body) hits.needs_description = 'sem texto da vaga';
  else if (v.body.length < c.minDesc && job.descriptionFinal !== true) hits.needs_description = `texto curto (${v.body.length} caracteres)`;

  const spain = has(c.spainPlaces, v.loc) || c.spainHead.some((r) => has(r, v.head));
  if (spain && !firstHit(c, v.full, c.spainForeign)) hits.spain_local = `vaga na Espanha (${v.loc || v.title}) sem EOR, contractor, freelance ou B2B no anúncio`;

  // Benefício genérico da empresa ('hybrid roles…; fully remote roles…') não diz se ESTA vaga é remota.
  const generic = remote.confirmed && has(c.remoteGenericEv, remote.evidence || '') && has(c.remoteGenericCtx, v.full);
  if (generic) remote = { ...remote, confirmed: false };
  const friendly = !generic && !remote.confirmed && has(c.remoteFriendly, v.full);
  if (!remote.confirmed && !friendly && !generic) hits.no_remote = 'nenhuma frase do anúncio diz que é remoto';

  const lang = languageOf(c, v);
  if (lang.evidence) hits.language = lang.evidence;

  const currency = currencyOf(c, job);
  if (currency.status === 'bad') hits.currency = currency.evidence;

  const contractor = contractorOf(c, job, v);

  const key = c.order.find((k) => k in hits);
  let exclude = null;
  if (key) {
    exclude = { key, label: R[key].label, ui: R[key].ui, evidence: hits[key] };
    if (key === 'learned') Object.assign(exclude, { label: `${R.learned.label}: ${learnedHit.label}`, ui: learnedHit.ui, rule: learnedHit.id });
  }
  const labels = [];
  if (contractor.type) labels.push(contractor.type);
  if (currency.status === 'unknown') labels.push('moeda?');
  if (friendly) labels.push('confirmar: remote friendly');
  if (generic) labels.push('confirmar: remoto genérico');
  if (contractor.type && has(c.spanishBase, v.body) && !has(c.foreignEntity, v.full)) labels.push('confirmar: freelance empresa espanhola');
  if (has(c.seniorityConfirm, v.title)) labels.push('confirmar: principal');
  return {
    exclude,
    remote: { confirmed: remote.confirmed, how: remote.how, evidence: remote.evidence },
    contractor, currency, country: countryIn(c, v.loc), lang: lang.code, labels, hits,
  };
}

// ---- API ----
const hardExclude = (job) => assess(job).exclude;
const hitOf = (key) => (job) => key in assess(job).hits;

module.exports = {
  RULES_FILE, criteria, normalize, plainText, assess, hardExclude,
  // Compatibilidade com quem já usava filters.js (job-monitor, clean-engage-pool, refetch-restore-jobs).
  hardExcludeReason: (job) => { const x = hardExclude(job); return x ? x.label : null; },
  // Só falta o texto da vaga pra decidir (nenhuma regra anterior já exclui).
  needsDescription: (job) => { const x = hardExclude(job); return !!x && x.key === 'needs_description'; },
  isSpainLocal: hitOf('spain_local'), isOnsite: hitOf('onsite'), needsRelocation: hitOf('relocation'), languageBlocked: hitOf('language'),
  confirmsRemote: (job) => assess(job).remote.confirmed,
  currencyStatus: (job) => assess(job).currency.status,
  isContractor: (job) => !!assess(job).contractor.type,
  labelsFor: (job) => assess(job).labels,
  detectLang: (text) => langOf(criteria(), normalize(text)).code,
  seniorityBlocked: (title) => { const c = criteria(); const h = firstHit(c, normalize(title), c.seniority); return h ? h.evidence : null; },
  countryOf: (location) => { const c = criteria(); return countryIn(c, normalize(location).replace(c.synthetic, '')); },
};

// Regex soltas que o código antigo testava direto (texto cru, sem caixa).
const joined = (list) => new RegExp(list.map((s) => `(?:${s})`).join('|'), 'i');
for (const [name, pick] of [
  ['CONTRACTOR_HINTS', (r) => r.contractor.types.map(([, s]) => s)],
  ['EXCLUDE_ONSITE_LOC', (r) => r.onsite.labels],
  ['EXCLUDE_ONSITE_DESC', (r) => r.onsite.explicit],
]) Object.defineProperty(module.exports, name, { enumerable: true, get: () => joined(pick(criteria().raw.rules)) });
