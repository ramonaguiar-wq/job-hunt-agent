// people.js - "Pessoas quentes" da vaga: buscas prontas no LinkedIn + nota de conexão (< 300 chars).
// LinkedIn não tem API pública de pessoas, então geramos buscas já filtradas pela empresa
// (abrem logado no seu LinkedIn) e extraímos nomes citados na própria descrição da vaga.

const NOTE_LIMIT = 300;

function prettyCompany(name) {
  const n = String(name || '').trim();
  // slug de ATS (ex: "elevenlabs", "grafanalabs") → capitaliza
  const KNOWN = { elevenlabs: 'ElevenLabs', posthog: 'PostHog', gitlab: 'GitLab', hubspot: 'HubSpot', grafanalabs: 'Grafana Labs', n8n: 'n8n', jobgether: 'Jobgether' };
  if (KNOWN[n.toLowerCase()]) return KNOWN[n.toLowerCase()];
  return n === n.toLowerCase() ? n.replace(/(^|[-_ ])\w/g, m => m.toUpperCase()).replace(/[-_]/g, ' ') : n;
}

function linkedinPeople(keywords) {
  return `https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(keywords)}&origin=GLOBAL_SEARCH_HEADER`;
}

function googleXray(query) {
  return `https://www.google.com/search?q=${encodeURIComponent(`site:linkedin.com/in ${query}`)}`;
}

// Nomes citados na JD: "reporting to Jane Doe", "Your recruiter: John Smith", "hiring manager, Ana Silva"
function namedContacts(text) {
  const re = /(?:report(?:ing|s)? (?:directly )?to|recruiter(?: for this role)?(?: is|:)|hiring manager(?: is|:|,)|talent partner(?: is|:)|contact)\s+([A-Z][a-zà-ÿ]+(?: [A-Z][a-zà-ÿ'-]+){1,2})/g;
  const out = new Map();
  let m;
  while ((m = re.exec(text))) {
    const name = m[1].trim();
    if (!/^(The|Our|A|An|Head|Director|Lead|VP)\b/.test(name)) out.set(name, m[0].split(/\s+/).slice(0, 2).join(' '));
  }
  return [...out].slice(0, 3).map(([name, ctx]) => ({ name, why: `Citado na vaga (“${ctx}…”)` }));
}

function trimNote(s) {
  return s.length <= NOTE_LIMIT ? s : s.slice(0, NOTE_LIMIT - 1).replace(/\s+\S*$/, '') + '…';
}

// Prova concreta por tema da vaga (métricas reais do perfil). Primeira na ordem de FIT que bater.
const PROOF = {
  'Retail Media / AdTech': { en: '+33.4% GMV redesigning iFood Ads auction', pt: '+33,4% GMV no leilão do iFood Ads' },
  'AI / LLM': { en: "designed iFood Ads' LLM insights product", pt: 'desenhei o produto de insights com LLM do iFood Ads' },
  'Fintech / pagamentos': { en: 'KYC + unified onboarding at Caju fintech', pt: 'KYC e onboarding unificado na Caju' },
  'Design System': { en: "ran Caju's design system (iF Award 2026)", pt: 'governei o design system da Caju (iF Award 2026)' },
  'B2B SaaS': { en: '+33.4% GMV redesigning iFood Ads auction', pt: '+33,4% GMV no leilão do iFood Ads' }
};

// Gatilhos (revisados com o Hermes): prova específica no início, curiosidade,
// pedido de baixa fricção, sem pedir indicação (reciprocidade). Notas <= 200 chars.
function buildNotes(job, company, fit) {
  const role = job.title.replace(/\s*[-–(|].*$/, '').trim();
  const p = PROOF[fit.find(f => PROOF[f])] || PROOF['Retail Media / AdTech'];
  const short = s => trimNote(s).slice(0, 200);
  return {
    recruiter: short(`Hi [Name], applied for ${role}. Senior PD, 11 yrs: ${p.en}. Remote from Valencia (EU). Glad to share anything that helps your review.`),
    designer: short(`Hi [Name], ${p.en}. Just applied to ${role} at ${company}. Curious: what's your team's biggest design bet this year?`),
    peer: short(`Hi [Name], fellow product designer. Applied to ${role} at ${company}. Not asking for a referral, just curious how design works there. Connect?`),
    brazilian: short(`Oi [Nome]! Brasileiro em Valência, ${p.pt}. Apliquei pra ${role} na ${company}. Me conta em 1 linha como é o time? Valeu!`),
    long: (
      `${p.en.charAt(0).toUpperCase() + p.en.slice(1)}. That's why ${role} at ${company} caught my eye.\n\n` +
      `Senior Product Designer, 11 yrs. iFood Ads: AI/LLM insights, auction redesign (+33.4% GMV, +23.1% CTR). ` +
      `Caju fintech: design system + onboarding behind an iF Design Award 2026 redesign.\n\n` +
      `I just applied. Not asking for a referral, just 10 min to see if I fit what you need. ` +
      `If not, one tip on how ${company} thinks about design would already help.\n\nThanks, ${process.env.CANDIDATE_NAME || 'Candidate'}`).slice(0, 600)
  };
}

function hotPeople(job, plainDescription, fit = []) {
  const company = prettyCompany(job.company);
  const q = `"${company}"`;
  return {
    company,
    named: namedContacts(plainDescription || ''),
    searches: [
      { label: 'Recrutador / Talent', why: 'Quem faz a triagem dos CVs', url: linkedinPeople(`${company} recruiter talent acquisition`) },
      { label: 'Senior Product Designer (IC)', why: 'Par sênior no time — IC, não gestor', url: linkedinPeople(`${company} "senior product designer" -"head of" -"design manager"`) },
      { label: 'Product Designers do time', why: 'Pares que podem indicar', url: linkedinPeople(`${company} "product designer" -"head of" -"design manager" -"design lead"`) },
      { label: 'Brasileiros na empresa', why: 'Contato mais quente (nota em PT)', url: googleXray(`${q} (Brasil OR Brazil OR "São Paulo")`) }
    ],
    notes: buildNotes(job, company, fit)
  };
}

module.exports = { hotPeople };
