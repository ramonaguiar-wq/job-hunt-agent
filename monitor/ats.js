// ats.js - Busca direto nos ATS das empresas (Greenhouse, Lever, Ashby).
// Nesses três dá pra aplicar sem criar conta: formulário com dados + CV + carta.
// Também tenta converter vaga de agregador (RemoteOK, Himalayas...) no link direto do ATS.

const BOARDS = {
  greenhouse: ['remotecom', 'typeform', 'gitlab', 'canonical', 'n26', 'wise', 'hubspot', 'cabify',
    'webflow', 'grafanalabs', 'elastic', 'mozilla', 'bitpanda', 'ebury'],
  lever: ['qonto', 'airalo', 'jobgether', 'remofirst', 'mistral', 'spotify', 'pigment', 'swile'],
  ashby: ['pliant', 'deel', 'ramp', 'linear', 'notion', 'vercel', 'supabase', 'posthog', 'n8n',
    'synthesia', 'elevenlabs', 'dust', 'lovable', 'attio', 'pennylane']
};

const TIMEOUT = 20000;

async function getJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(TIMEOUT) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function greenhouse(slug, titleFilter) {
  const data = await getJson(`https://boards-api.greenhouse.io/v1/boards/${slug}/jobs`);
  const hits = (data.jobs || []).filter(j => titleFilter(j.title));
  // descrição só pras que passaram no filtro de título (a lista com content=true é pesada)
  return Promise.all(hits.map(async j => {
    let content = '';
    try {
      content = (await getJson(`https://boards-api.greenhouse.io/v1/boards/${slug}/jobs/${j.id}`)).content || '';
    } catch (_) {}
    const loc = j.location?.name || '';
    return {
      id: `gh-${slug}-${j.id}`,
      title: j.title,
      company: j.company_name || slug,
      url: j.absolute_url,
      location: loc,
      remote: /remote|anywhere/i.test(loc),
      description: content.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&'),
      ats: 'Greenhouse',
      source: 'Greenhouse'
    };
  }));
}

async function lever(slug, titleFilter) {
  const data = await getJson(`https://api.lever.co/v0/postings/${slug}?mode=json`);
  return (Array.isArray(data) ? data : []).filter(j => titleFilter(j.text)).map(j => {
    const locs = j.categories?.allLocations?.join(', ') || j.categories?.location || '';
    return {
      id: `lever-${slug}-${j.id}`,
      title: j.text,
      company: slug,
      url: j.applyUrl || j.hostedUrl,
      location: locs,
      remote: j.workplaceType === 'remote' || /remote/i.test(locs),
      description: j.description + (j.lists || []).map(l => `<h3>${l.text}</h3><ul>${l.content}</ul>`).join('') + (j.additional || ''),
      tags: j.categories?.commitment,
      postedAt: j.createdAt,
      ats: 'Lever',
      source: 'Lever'
    };
  });
}

async function ashby(slug, titleFilter) {
  const data = await getJson(`https://api.ashbyhq.com/posting-api/job-board/${slug}?includeCompensation=true`);
  return (data.jobs || []).filter(j => j.isListed !== false && titleFilter(j.title)).map(j => {
    const locs = [j.location, ...(j.secondaryLocations || []).map(l => l.location)].filter(Boolean).join(', ');
    return {
      id: `ashby-${slug}-${j.id}`,
      title: j.title,
      company: slug,
      url: j.applyUrl || j.jobUrl,
      location: locs,
      // isRemote vem true até pra híbrido; workplaceType é o confiável
      remote: j.workplaceType === 'Remote',
      description: j.descriptionHtml || j.descriptionPlain,
      salary: j.compensation?.compensationTierSummary || '',
      tags: j.employmentType,
      postedAt: Date.parse(j.publishedAt) || null,
      ats: 'Ashby',
      source: 'Ashby'
    };
  });
}

const FETCHERS = { greenhouse, lever, ashby };

// onError(texto): recebe o erro de cada board (o job-monitor grava em collect-status.json).
async function fetchAtsJobs(titleFilter, onError) {
  const tasks = [];
  for (const [ats, slugs] of Object.entries(BOARDS)) {
    for (const slug of slugs) {
      tasks.push(FETCHERS[ats](slug, titleFilter).catch(err => {
        console.error(`[${ats}:${slug}] erro: ${err.message}`);
        if (onError) onError(`${ats}:${slug} ${err.message}`);
        return [];
      }));
    }
  }
  return (await Promise.all(tasks)).flat();
}

// Vaga de agregador → procura a mesma vaga no ATS da empresa (slug = nome da empresa)
function slugCandidates(company) {
  const base = String(company || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/\b(inc|ltd|llc|gmbh|sa|sl|bv|ag|co)\b\.?/g, '').trim();
  return [...new Set([base.replace(/[^a-z0-9]/g, ''), base.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')])].filter(Boolean);
}

function normTitle(t) {
  return String(t || '').toLowerCase().replace(/\(.*?\)|[^a-z ]/g, ' ').replace(/\s+/g, ' ').trim();
}

async function resolveDirect(job) {
  const want = normTitle(job.title);
  const sameTitle = t => {
    const n = normTitle(t);
    return n === want || n.includes(want) || want.includes(n);
  };
  for (const slug of slugCandidates(job.company)) {
    for (const [ats, fn] of Object.entries(FETCHERS)) {
      try {
        const found = await fn(slug, sameTitle);
        if (found.length) {
          const f = found[0];
          return { ...job, url: f.url, description: f.description || job.description, ats: f.ats, via: job.source };
        }
      } catch (_) { /* board não existe nesse ATS */ }
    }
  }
  return job;
}

const unescapeHtml = (h) => String(h || '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const htmlToText = (h) => unescapeHtml(String(h || '').replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ')
  .replace(/<(br|\/p|\/li|\/h\d)\s*\/?>/gi, '\n').replace(/<[^>]+>/g, ' ')).replace(/&nbsp;/g, ' ')
  .replace(/[ \t]+/g, ' ').replace(/\s*\n\s*/g, '\n').trim();

// Texto completo da vaga a partir do job.url (Greenhouse / Lever / Ashby); '' se não reconhece a URL.
async function fetchFromAtsUrl(url) {
  let m = String(url || '').match(/greenhouse\.io\/(?:v1\/boards\/)?([^/?#]+)\/jobs\/(\d+)/) ||
    String(url || '').match(/greenhouse\.io\/([^/?#]+)\/?.*[?&]gh_jid=(\d+)/);
  if (m) {
    const d = await getJson(`https://boards-api.greenhouse.io/v1/boards/${m[1]}/jobs/${m[2]}`);
    return { description: unescapeHtml(d.content || ''), location: d.location?.name || '' };
  }
  m = String(url || '').match(/jobs\.(?:eu\.)?lever\.co\/([^/?#]+)\/([0-9a-f-]{36})/);
  if (m) {
    const j = await getJson(`https://api.lever.co/v0/postings/${m[1]}/${m[2]}`);
    return {
      description: (j.description || '') + (j.lists || []).map(l => `<h3>${l.text}</h3><ul>${l.content}</ul>`).join('') + (j.additional || ''),
      location: j.categories?.allLocations?.join(', ') || j.categories?.location || ''
    };
  }
  m = String(url || '').match(/jobs\.ashbyhq\.com\/([^/?#]+)\/([0-9a-f-]{36})/);
  if (m) {
    const d = await getJson(`https://api.ashbyhq.com/posting-api/job-board/${m[1]}?includeCompensation=true`);
    const j = (d.jobs || []).find(x => x.id === m[2]);
    if (j) return { description: j.descriptionHtml || j.descriptionPlain || '', location: j.location || '' };
  }
  return null;
}

// Busca o texto completo da vaga (sem inventar flags de remoto). Devolve { ...job, description, location? }.
async function fetchJobDescription(job) {
  const keep = (d, location) => (d && String(d).trim().length > String(job.description || '').length
    ? { ...job, description: d, ...(location && !job.location ? { location } : {}) } : null);
  try {
    const f = await fetchFromAtsUrl(job.url);
    const r = f && keep(f.description, f.location);
    if (r) return r;
  } catch (_) { /* cai pro próximo método */ }
  if (/^linkedin-\d+/.test(job.id || '') || /linkedin\.com\/jobs\/view/.test(job.url || '')) {
    try {
      const r = keep(await require('./linkedin').fetchLinkedInJobDescription(job));
      if (r) return r;
    } catch (_) {}
  }
  try {
    const r = await resolveDirect(job);
    if (r.description && r.description !== job.description) return { ...job, description: r.description, url: r.url, ats: r.ats };
  } catch (_) {}
  if (/^https?:/.test(job.url || '')) {
    try {
      const res = await fetch(job.url, { headers: { 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36' }, signal: AbortSignal.timeout(TIMEOUT) });
      if (res.ok) { const r = keep(htmlToText(await res.text())); if (r) return r; }
    } catch (_) { /* 403 etc: best-effort */ }
  }
  return job;
}

module.exports = { fetchAtsJobs, resolveDirect, fetchJobDescription, BOARDS };
