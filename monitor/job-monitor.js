#!/usr/bin/env node
// job-monitor.js - Bot automático de busca de vagas
// Roda sozinho (via launchd), busca em APIs públicas reais de vagas,
// filtra por palavras-chave, e notifica só o que é NOVO.
//
// Uso manual: node job-monitor.js
// Uso automático: instalado via launchd (ver install-job-monitor.sh)

require('dotenv').config({ path: require('path').join(__dirname, '.env') });
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { fetchAtsJobs, resolveDirect, fetchJobDescription } = require('./ats');
const { fetchLinkedInJobs, fetchLinkedInJobDetail, linkedInStats, Blocked } = require('./linkedin');
const { hotPeople } = require('./people');
const { rankJobs, fitReasons } = require('./rank');
const F = require('./filters'); // regras duras de job-criteria.json (as mesmas do painel, via criteria.py)

const SEEN_FILE = path.join(__dirname, 'data', 'seen-jobs.json');
const LOG_FILE = path.join(__dirname, 'data', 'jobs-found.log');
const REPORT_FILE = path.join(__dirname, 'data', 'latest-jobs.html');
// Fila ranqueada que o linkedin-agent consome pra montar os alvos de engajamento
const ENGAGE_FILE = path.join(__dirname, 'data', 'engage-jobs.json');
const ENGAGE_KEEP_DAYS = 14;
const ENGAGE_PER_RUN = 5;

const KEYWORDS = [
  'product designer',
  'product design',
  'ux designer',
  'ux/ui',
  'ui/ux',
  'design engineer',
  'ai design',
  'design system'
];

// Regras duras (senioridade, remoto, país, presencial, relocation, autorização, Espanha local, idioma, moeda)
// moram em job-criteria.json e rodam pelo filters.js; o painel usa as mesmas pelo criteria.py.
// Revisão 2026-10-16: saíram EXCLUDE_TITLE, EXCLUDE_ONLY, ALLOWED_LOCATIONS, RESIDENCY_* e isRemoteCompatible.

// Empresas onde o candidato já aplicou (mesma lista das varreduras do Hermes)
const APPLIED = /\b(aios|injective|qonto|remofirst|remote\.?com|remotecom|bitpanda|ioent|ebury)\b/i;

// "Por que combina" (FIT_SIGNALS/fitReasons) mudou pro rank.js em 2026-10-16: o clean-engage-pool recalcula com o texto completo.

// Vaga sem texto não é descartada: espera a próxima rodada (fica fora do seen). Depois de MAX_TEXT_TRIES
// rodadas sem texto (ou MAX_AGE_DAYS esperando) entra no pool marcada needsDescription, e o painel mostra à
// parte pra abrir na mão. A vaga fica guardada no arquivo e volta pra fila mesmo que a busca da rodada não a traga.
const PENDING_FILE = path.join(__dirname, 'data', 'pending-description.json');
const MAX_TEXT_TRIES = 3;
const PENDING_KEEP = ['id', 'title', 'company', 'url', 'location', 'source', 'postedAt', 'geo', 'remoteTag', 'ats', 'salary', 'tags', 'description'];
// Páginas públicas do LinkedIn por rodada (o resto espera a próxima, sem gastar tentativa) e pausa entre elas.
const MAX_LINKEDIN_DETAILS = 40;
const linkedInPause = () => new Promise(r => setTimeout(r, 4000 + Math.random() * 5000));
// Tamanho máximo do texto guardado. A coleta decide sobre o texto já cortado, então o painel decide igual.
const MAX_TEXT = 15000;
// Última coleta: hora, números por fonte e erros. O painel do linkedin-agent mostra.
const STATUS_FILE = path.join(__dirname, 'data', 'collect-status.json');
const NOT_A_RULE = { applied: 'já aplicou na empresa' };

// Janela de busca: vagas publicadas nos últimos N dias
const MAX_AGE_DAYS = 7;
const CUTOFF = Date.now() - MAX_AGE_DAYS * 24 * 3600 * 1000;
const toMs = (v) => v == null ? null : (typeof v === 'number' ? (v < 1e12 ? v * 1000 : v) : (Date.parse(v) || null));

// Fontes públicas, sem necessidade de API key
// `page`: fontes paginadas; busca páginas até passar da janela de MAX_AGE_DAYS (ou maxPages)
const SOURCES = [
  {
    name: 'RemoteOK',
    url: 'https://remoteok.com/api',
    parse: (data) => {
      // primeiro item é metadata, ignora
      return (Array.isArray(data) ? data.slice(1) : []).map(j => ({
        id: `remoteok-${j.id}`,
        title: j.position || j.title,
        company: j.company,
        url: j.url || `https://remoteok.com/remote-jobs/${j.id}`,
        location: j.location || '',
        remote: true,
        description: j.description,
        postedAt: toMs(j.epoch || j.date),
        source: 'RemoteOK'
      }));
    }
  },
  {
    name: 'Remotive',
    url: 'https://remotive.com/api/remote-jobs?category=design',
    parse: (data) => {
      return (data.jobs || []).map(j => ({
        id: `remotive-${j.id}`,
        title: j.title,
        company: j.company_name,
        url: j.url,
        location: j.candidate_required_location || '',
        remote: true,
        description: j.description,
        tags: j.job_type,
        postedAt: toMs(j.publication_date),
        source: 'Remotive'
      }));
    }
  },
  {
    name: 'Arbeitnow',
    // 3 páginas cobriam só ~1 dia; pagina até cobrir a janela
    page: (p) => `https://www.arbeitnow.com/api/job-board-api?page=${p}`,
    maxPages: 25,
    parse: (data) => {
      return (data.data || []).map(j => ({
        id: `arbeitnow-${j.slug}`,
        title: j.title,
        company: j.company_name,
        url: j.url,
        location: j.remote ? '' : (j.location || ''),
        remote: j.remote === true,
        description: j.description,
        tags: (j.job_types || []).join(' '),
        postedAt: toMs(j.created_at),
        source: 'Arbeitnow'
      }));
    }
  },
  {
    name: 'Himalayas',
    // endpoint de busca: antes só vinham as 20 vagas mais recentes de qualquer área
    page: ['product designer', 'ux designer'].map(q => (p) =>
      `https://himalayas.app/jobs/api/search?q=${encodeURIComponent(q)}&sort=recent&page=${p}`),
    maxPages: 10,
    parse: (data) => {
      return (data.jobs || []).map(j => ({
        id: `himalayas-${j.guid || j.id}`,
        title: j.title,
        company: j.companyName,
        url: j.applicationLink || j.url,
        location: (j.locationRestrictions || []).join(', '),
        remote: true,
        description: j.description || j.excerpt,
        tags: j.employmentType,
        postedAt: toMs(j.pubDate),
        salary: j.minSalary ? `${j.currency || ''} ${j.minSalary}–${j.maxSalary || ''}/${j.salaryPeriod || 'year'}` : '',
        source: 'Himalayas'
      }));
    }
  },
  // InfoJobs removido em 2026-10-15: só trazia vaga local espanhola (fora do alvo).
  {
    // RSS público — board de design mais forte p/ Product/UX (the candidate tem conta).
    // Título: "Company is hiring for a position of TITLE in LOCATION"
    name: 'Dribbble',
    rss: true,
    url: 'https://dribbble.com/jobs.rss',
    parse: (xml) => parseRssItems(xml).map(it => {
      const raw = decodeXml(it.title || '');
      const m = raw.match(/^(.*?)\s+is hiring for a position of\s+(.+?)\s+in\s+(.+)$/i);
      const company = decodeXml(it.creator || (m ? m[1] : '') || 'Unknown');
      const title = m ? m[2].trim() : raw;
      const location = m ? m[3].trim() : '';
      const remote = /\bremote|worldwide|anywhere|work from home\b/i.test(location) || !location;
      const url = (it.link || '').split('?')[0];
      const guid = (it.guid || url).replace(/\?.*$/, '');
      return {
        id: `dribbble-${guid}`,
        title,
        company,
        url,
        location,
        remote,
        description: decodeXml(it.description || ''),
        postedAt: toMs(it.pubDate),
        source: 'Dribbble'
      };
    })
  },
  {
    // RSS Design + UX/UI + Product Design. WWR mistura lixo na categoria Design —
    // o filtro de KEYWORDS no main() limpa.
    name: 'WeWorkRemotely',
    rss: true,
    url: [
      'https://weworkremotely.com/categories/remote-design-jobs.rss',
      'https://weworkremotely.com/categories/remote-ux-ui-design-jobs.rss',
      'https://weworkremotely.com/categories/remote-product-design-jobs.rss'
    ],
    parse: (xml) => parseRssItems(xml).map(it => {
      const raw = decodeXml(it.title || '');
      // "Company: Job Title" (padrão WWR)
      const m = raw.match(/^(.+?):\s*(.+)$/);
      const company = m ? m[1].trim() : '';
      const title = m ? m[2].trim() : raw;
      const location = decodeXml(it.region || it.country || '') || 'Remote';
      const url = (it.link || '').trim();
      return {
        id: `wwr-${it.guid || url}`,
        title,
        company,
        url,
        location,
        remote: true,
        description: decodeXml(it.description || ''),
        tags: [it.type, it.category].filter(Boolean).join(' '),
        postedAt: toMs(it.pubDate),
        source: 'WeWorkRemotely'
      };
    })
  }
];

function decodeXml(s) {
  return String(s || '')
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .trim();
}

function parseRssItems(xml) {
  const items = [];
  const re = /<item>([\s\S]*?)<\/item>/gi;
  let m;
  while ((m = re.exec(xml))) {
    const block = m[1];
    const tag = (name) => {
      const r = new RegExp('<(?:' + name + '|dc:' + name + ')(?:\\s[^>]*)?>([\\s\\S]*?)</(?:' + name + '|dc:' + name + ')>', 'i');
      const hit = block.match(r);
      return hit ? hit[1].trim() : '';
    };
    let link = tag('link');
    if (!link) {
      const href = block.match(/<link[^>]+href=["']([^"']+)["']/i);
      link = href ? href[1] : '';
    }
    items.push({
      title: tag('title'),
      link,
      guid: tag('guid') || link,
      description: tag('description'),
      pubDate: tag('pubDate'),
      creator: tag('creator'),
      region: tag('region'),
      country: tag('country'),
      category: tag('category'),
      type: tag('type')
    });
  }
  return items;
}


function ensureDataDir() {
  const dir = path.join(__dirname, 'data');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function loadSeen() {
  ensureDataDir();
  if (!fs.existsSync(SEEN_FILE)) return new Set();
  try {
    const arr = JSON.parse(fs.readFileSync(SEEN_FILE, 'utf8'));
    return new Set(arr);
  } catch {
    return new Set();
  }
}

function saveSeen(seenSet) {
  ensureDataDir();
  // Desde 2026-10-16 a vaga descartada também entra no seen (id + chave), pra não buscar a página dela de novo.
  // 2000 cobria menos de um dia de LinkedIn; 50000 cobre semanas, bem mais que a janela de MAX_AGE_DAYS.
  const arr = Array.from(seenSet).slice(-50000);
  fs.writeFileSync(SEEN_FILE, JSON.stringify(arr));
}

function matchesKeywords(job) {
  const text = (job.title || '').toLowerCase();
  return KEYWORDS.some(k => text.includes(k));
}

async function fetchUrl(source, url) {
  if (source.html || source.rss) {
    // InfoJobs/alguns RSS: curl passa melhor que fetch do Node (TLS/UA).
    const { execFileSync } = require('child_process');
    const out = execFileSync('curl', ['-sL', '-m', '20', '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36', '-w', '\n%{http_code}', url], { maxBuffer: 20 * 1024 * 1024 }).toString();
    const code = out.slice(out.lastIndexOf('\n') + 1).trim();
    if (code !== '200') throw new Error(`HTTP ${code}`);
    return source.parse(out.slice(0, out.lastIndexOf('\n')));
  }
  const res = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0 (job-monitor-bot)' },
    signal: AbortSignal.timeout(20000)
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return source.parse(await res.json());
}

async function fetchSource(source, errors = []) {
  const jobs = [];
  if (source.page) {
    for (const pageUrl of [].concat(source.page)) {
      for (let p = 1; p <= source.maxPages; p++) {
        if (p > 1) await new Promise(r => setTimeout(r, 1500)); // evita HTTP 429 na paginação
        try {
          const batch = await fetchUrl(source, pageUrl(p));
          jobs.push(...batch);
          const oldest = Math.min(...batch.map(j => j.postedAt || Infinity));
          if (!batch.length || oldest < CUTOFF) break;
        } catch (err) {
          console.error(`[${source.name}] erro: ${err.message}`);
          errors.push(err.message);
          break;
        }
      }
    }
    return jobs;
  }
  const urls = Array.isArray(source.url) ? source.url : [source.url];
  for (const url of urls) {
    try {
      if (source.rss || source.html) {
        jobs.push(...await fetchUrl(source, url));
      } else {
        const res = await fetch(url, {
          headers: { 'User-Agent': 'Mozilla/5.0 (job-monitor-bot)' },
          signal: AbortSignal.timeout(20000)
        });
        if (!res.ok) {
          console.error(`[${source.name}] HTTP ${res.status}`);
          errors.push(`HTTP ${res.status}`);
          continue;
        }
        jobs.push(...source.parse(await res.json()));
      }
    } catch (err) {
      console.error(`[${source.name}] erro: ${err.message}`);
      errors.push(err.message);
    }
  }
  return jobs;
}

// Mesma vaga repostada/listada em várias fontes (ex: SumUp 2x no Arbeitnow)
function dedupeKey(job) {
  return `${job.title} ${job.company}`.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function stripHtml(html) {
  return String(html || '').replace(/<(br|\/p|\/li|\/h\d)[^>]*>/gi, '\n').replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&#39;|&rsquo;/g, "'").replace(/&quot;/g, '"')
    .replace(/\n{3,}/g, '\n\n').trim();
}

function writeReport(jobs) {
  ensureDataDir();
  // contractor/EOR primeiro
  const sorted = [...jobs].sort((a, b) =>
    Number(!!b.ats) - Number(!!a.ats) || Number(b.contractor) - Number(a.contractor));
  const rows = sorted.map(j => `
    <details class="job">
      <summary>
        <strong>${escapeHtml(j.title)}</strong> — ${escapeHtml(j.company)}
        ${j.ats ? `<span class="tag">Aplicação direta · ${escapeHtml(j.ats)}</span>` : '<span class="tag warn">Agregador — pode pedir cadastro</span>'}
        ${j.contractor ? '<span class="tag">Contractor/EOR</span>' : ''}
        <div class="meta">${escapeHtml(j.source)} · ${escapeHtml(j.location || 'Worldwide')}${j.salary ? ' · ' + escapeHtml(j.salary) : ''}</div>
      </summary>
      ${j.fit?.length ? `<div class="fit">🎯 Por que combina: ${escapeHtml(j.fit.join(', '))}</div>` : ''}
      <div class="desc">${escapeHtml(stripHtml(j.description) || 'Sem descrição na fonte — abra o link.')}</div>
      ${j.people ? `<div class="people"><strong>👥 Pessoas quentes</strong><ul>
        ${j.people.named.map(n => `<li><strong>${escapeHtml(n.name)}</strong> — ${escapeHtml(n.why)}</li>`).join('')}
        ${j.people.searches.map(s => `<li><a href="${escapeHtml(s.url)}" target="_blank">${escapeHtml(s.label)}</a> — ${escapeHtml(s.why)}</li>`).join('')}
        </ul>
        <div class="note"><em>Recrutador:</em> ${escapeHtml(j.people.notes.recruiter)}</div>
        <div class="note"><em>Senior IC:</em> ${escapeHtml(j.people.notes.designer)}</div>
        <div class="note"><em>Designer do time:</em> ${escapeHtml(j.people.notes.peer)}</div>
        <div class="note"><em>Brasileiro:</em> ${escapeHtml(j.people.notes.brazilian)}</div>
        <div class="note" style="white-space:pre-wrap"><em>Mensagem longa:</em>\n${escapeHtml(j.people.notes.long)}</div></div>` : ''}
      <a class="apply" href="${escapeHtml(j.url)}" target="_blank">${j.ats ? 'Aplicar direto ↗' : 'Abrir vaga ↗'}</a>
    </details>`).join('\n');

  const html = `<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8">
<title>Novas vagas encontradas</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; background: #111; color: #eee; }
  h1 { font-size: 20px; }
  .job { padding: 14px; margin-bottom: 10px; background: #1c1c1e; border-radius: 10px; }
  summary { cursor: pointer; font-size: 16px; }
  .meta { color: #999; font-size: 13px; margin-top: 4px; }
  .tag { background: #1f6f3f; color: #cfe; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }
  .desc { white-space: pre-wrap; color: #ccc; font-size: 14px; line-height: 1.5; margin: 12px 0; max-height: 480px; overflow: auto; }
  .apply { color: #4da3ff; }
  .tag.warn { background: #6f4a1f; color: #fed; }
  .people { background: #26262a; border-radius: 8px; padding: 10px 12px; margin: 10px 0; font-size: 14px; }
  .people a { color: #4da3ff; }
  .note { user-select: all; background: #1a1a1c; padding: 8px; border-radius: 6px; margin-top: 6px; font-size: 13px; color: #ddd; }
  .fit { color: #9fd; font-size: 13px; margin-top: 10px; }
</style></head>
<body>
  <h1>🔍 ${jobs.length} vaga(s) nova(s) — ${new Date().toLocaleString('pt-BR')}</h1>
  <p class="meta">Clique numa vaga para ver a descrição completa.</p>
  ${rows}
</body></html>`;

  fs.writeFileSync(REPORT_FILE, html);
  return REPORT_FILE;
}

// Mesma notificação do Telegram, enviada também ao WhatsApp via bridge do Hermes (self-chat)
async function sendWhatsApp(jobs) {
  const chatId = process.env.WHATSAPP_NOTIFY_CHAT || '';
  if (!chatId) return; // notificação WhatsApp é opcional
  const bridge = process.env.WHATSAPP_BRIDGE_URL || 'http://127.0.0.1:3000/send';
  for (const j of jobs.filter(j => j.ats).slice(0, 15)) {
    const desc = stripHtml(j.description).slice(0, 400);
    const text = `💼 *${j.title}* — ${j.company}${j.contractor ? ' ✅ Contractor/EOR' : ''}\n` +
      `📍 ${j.location || 'Worldwide'} · ${j.source}${j.salary ? ' · ' + j.salary : ''}\n` +
      (j.fit?.length ? `🎯 Por que combina: ${j.fit.join(', ')}\n` : '') +
      `\n${desc}${desc.length === 400 ? '…' : ''}\n\n📝 Aplicar (${j.ats}, sem criar conta):\n${j.url}`;
    try {
      const res = await fetch(bridge, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chatId, message: text })
      });
      if (!res.ok) console.error(`[WhatsApp] HTTP ${res.status}`);
    } catch (err) {
      console.error(`[WhatsApp] erro: ${err.message}`);
    }
  }
}

async function sendTelegram(jobs) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) return;
  // só vaga com link direto de aplicação; o resto fica no relatório HTML
  for (const j of jobs.filter(j => j.ats).slice(0, 15)) {
    const desc = stripHtml(j.description).slice(0, 700);
    const text = `<b>${escapeHtml(j.title)}</b> — ${escapeHtml(j.company)}${j.contractor ? ' ✅ Contractor/EOR' : ''}\n` +
      `📍 ${escapeHtml(j.location || 'Worldwide')} · ${j.source}${j.salary ? ' · ' + escapeHtml(j.salary) : ''}\n\n` +
      (j.fit?.length ? `🎯 Por que combina: ${escapeHtml(j.fit.join(', '))}\n\n` : '') +
      `${escapeHtml(desc)}${desc.length === 700 ? '…' : ''}\n\n<a href="${escapeHtml(j.url)}">📝 Aplicar direto (${j.ats}, sem criar conta)</a>`;
    const p = j.people;
    const peopleText = p ? `👥 <b>Pessoas quentes — ${escapeHtml(p.company)}</b>\n` +
      p.named.map(n => `• <b>${escapeHtml(n.name)}</b> — ${escapeHtml(n.why)}\n`).join('') +
      p.searches.map(s => `• <a href="${escapeHtml(s.url)}">${escapeHtml(s.label)}</a> — ${escapeHtml(s.why)}`).join('\n') +
      `\n\n✉️ <b>Nota p/ recrutador:</b>\n<code>${escapeHtml(p.notes.recruiter)}</code>` +
      `\n\n✉️ <b>Nota p/ Senior IC:</b>\n<code>${escapeHtml(p.notes.designer)}</code>` +
      `\n\n✉️ <b>Nota p/ designer do time:</b>\n<code>${escapeHtml(p.notes.peer)}</code>` +
      `\n\n🇧🇷 <b>Nota p/ brasileiro:</b>\n<code>${escapeHtml(p.notes.brazilian)}</code>` +
      `\n\n💬 <b>Mensagem longa (InMail / perfil aberto):</b>\n<code>${escapeHtml(p.notes.long)}</code>` : '';
    for (const msg of [text, peopleText].filter(Boolean)) {
      try {
        await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ chat_id: chatId, text: msg, parse_mode: 'HTML', disable_web_page_preview: true })
        });
      } catch (err) {
        console.error(`[Telegram] erro: ${err.message}`);
      }
    }
  }
}

function notifyMac(title, message, openPath) {
  try {
    // Notificação passiva (aparece e some sozinha)
    const notifyScript = `display notification ${JSON.stringify(message)} with title ${JSON.stringify(title)} sound name "Glass"`;
    execFileSync('osascript', ['-e', notifyScript]);

    // Diálogo com botão "Ver Vagas" que abre a página direto — clique real, sem depender
    // de permissão de Notification Center (que apps ad-hoc não conseguem registrar no macOS atual)
    if (openPath) {
      const dialogScript = `
        set userChoice to display dialog ${JSON.stringify(message)} with title ${JSON.stringify(title)} buttons {"Depois", "Ver Vagas"} default button "Ver Vagas" giving up after 120
        if button returned of userChoice is "Ver Vagas" then
          do shell script "open " & quoted form of "${openPath}"
        end if
      `;
      execFileSync('osascript', ['-e', dialogScript]);
    }
  } catch (_) {
    // notificação/diálogo falhou (ex: sessão sem GUI), não é crítico
  }
}

function appendLog(jobs) {
  ensureDataDir();
  const timestamp = new Date().toISOString();
  const lines = jobs.map(j =>
    `[${timestamp}] ${j.source} | ${j.title} @ ${j.company} | ${j.location} | ${j.url}`
  ).join('\n') + '\n';
  fs.appendFileSync(LOG_FILE, lines);
}

// Guarda as vagas ranqueadas num pool rotativo de ENGAGE_KEEP_DAYS dias.
// O linkedin-agent lê esse arquivo; ele nunca fala com as APIs de vaga direto.
// Cada vaga leva o que as regras precisam pra decidir de novo no painel: o texto sobre o qual a coleta decidiu,
// o local real e, no LinkedIn, senioridade, tipo de contrato e se fechou.
function writeEngagePool(ranked) {
  ensureDataDir();
  let pool = [];
  try {
    pool = JSON.parse(fs.readFileSync(ENGAGE_FILE, 'utf8')).jobs || [];
  } catch (_) {
    pool = [];
  }
  const cutoff = Date.now() - ENGAGE_KEEP_DAYS * 24 * 3600 * 1000;
  const byId = new Map();
  for (const j of pool) {
    if ((j.addedAt || 0) >= cutoff) byId.set(j.id, j);
  }
  for (const j of ranked) {
    byId.set(j.id, {
      id: j.id,
      title: j.title,
      company: j.company,
      url: j.url,
      location: j.location || '',
      source: j.source,
      ats: j.ats || null,
      fit: j.fit || [],
      score: j.score,
      scoreReasons: j.scoreReasons || [],
      contractor: !!j.contractor,
      labels: j.labels || [],
      postedAt: j.postedAt || null,
      description: String(j.description || '').slice(0, MAX_TEXT),
      seniority: j.seniority || '',
      employmentType: j.employmentType || '',
      closed: !!j.closed,
      ...(j.needsDescription ? { needsDescription: true } : {}),
      people: j.people || null,
      addedAt: Date.now()
    });
  }
  const jobs = [...byId.values()].sort((a, b) => (b.score - a.score) || (b.addedAt - a.addedAt));
  fs.writeFileSync(ENGAGE_FILE, JSON.stringify({ updatedAt: Date.now(), jobs }, null, 2));
  console.error(`   Pool de engajamento: ${jobs.length} vagas (${ranked.length} novas ranqueadas)`);
  return { size: jobs.length, added: ranked.length };
}

const isLinkedIn = (j) => /^linkedin-\d+/.test(j.id || '') || /linkedin\.com\/jobs\/view/.test(j.url || '');
// Texto como o pool guarda (HTML vira texto, corte em MAX_TEXT). Toda decisão é sobre ele.
const canon = (j) => ({ ...j, description: F.plainText(j.description).slice(0, MAX_TEXT) });
const ruleLabel = (key) => NOT_A_RULE[key] || (F.criteria().raw.rules[key] || {}).label || key;

function loadPending() {
  try { return JSON.parse(fs.readFileSync(PENDING_FILE, 'utf8')); } catch (_) { return {}; }
}
function savePending(pending) {
  const cutoff = Date.now() - ENGAGE_KEEP_DAYS * 24 * 3600 * 1000;
  for (const [id, p] of Object.entries(pending)) if ((p.firstAt || 0) < cutoff) delete pending[id];
  fs.writeFileSync(PENDING_FILE, JSON.stringify(pending, null, 2));
}
const pendingSnapshot = (j) => Object.fromEntries(PENDING_KEEP.filter(k => j[k] != null && j[k] !== '').map(k => [k, j[k]]));

// Busca o texto da vaga. LinkedIn: página pública, que traz também o local real, a senioridade, o tipo de contrato
// e se a vaga fechou; se vier sem texto, tenta a mesma vaga no ATS da empresa. Outras fontes: ats.js.
// deferred = o LinkedIn pediu pausa ou acabou a cota da rodada (a vaga espera sem gastar tentativa).
async function withText(j, li) {
  if (!isLinkedIn(j)) return { job: canon(await fetchJobDescription(j)) };
  if (li.blocked || li.used >= MAX_LINKEDIN_DETAILS) return { job: j, deferred: true };
  if (li.used++) await linkedInPause();
  let d = null;
  try {
    d = await fetchLinkedInJobDetail(j);
  } catch (err) {
    if (err instanceof Blocked) { li.blocked = true; return { job: j, deferred: true }; }
    linkedInStats.errors.push(`página da vaga ${j.id}: ${err.message}`);
  }
  let job = d ? { ...j, description: d.description || j.description, location: d.location || j.location,
    seniority: d.seniority || '', employmentType: d.employmentType || '', closed: !!d.closed } : j;
  if (!String(job.description || '').trim() && !job.closed) {
    try { const r = await resolveDirect(job); if (r.description) job = r; } catch (_) { /* fica sem texto */ }
  }
  return { job: canon(job) };
}

function writeStatus(status) {
  ensureDataDir();
  let prev = {};
  try { prev = JSON.parse(fs.readFileSync(STATUS_FILE, 'utf8')); } catch (_) { /* primeira rodada */ }
  const errorCount = status.sources.reduce((n, s) => n + (s.errors || []).length, 0) + (status.error ? 1 : 0);
  const run = { at: status.finishedAt, ok: status.ok, new: status.counts.new || 0, errors: errorCount };
  fs.writeFileSync(STATUS_FILE, JSON.stringify({
    ...status, errorCount, durationMs: Date.parse(status.finishedAt) - Date.parse(status.startedAt),
    lastOkAt: status.ok ? status.finishedAt : (prev.lastOkAt || null),
    history: [run, ...(prev.history || [])].slice(0, 48)
  }, null, 2));
}

async function collect(status) {
  const seen = loadSeen();
  const pending = loadPending();
  const allJobs = [];

  for (const source of SOURCES) {
    const errors = [];
    const jobs = await fetchSource(source, errors);
    console.error(`   ${source.name}: ${jobs.length} vagas encontradas`);
    status.sources.push({ name: source.name, found: jobs.length, errors });
    allJobs.push(...jobs);
  }

  let linkedInJobs = [];
  try {
    linkedInJobs = await fetchLinkedInJobs(MAX_AGE_DAYS);
  } catch (err) {
    linkedInStats.errors.push(err.message);
  }
  console.error(`   LinkedIn (público, sem login): ${linkedInJobs.length} vagas encontradas`);
  const liStatus = { name: 'LinkedIn', found: linkedInJobs.length };
  status.sources.push(liStatus);
  allJobs.push(...linkedInJobs);

  const atsErrors = [];
  const atsJobs = await fetchAtsJobs(t => matchesKeywords({ title: t }), (e) => atsErrors.push(e));
  console.error(`   ATS direto (Greenhouse/Lever/Ashby): ${atsJobs.length} vagas de design`);
  status.sources.push({ name: 'ATS direto', found: atsJobs.length, errors: atsErrors });
  allJobs.push(...atsJobs);

  const excluded = status.excluded;
  const count = (key) => { excluded[key] = (excluded[key] || 0) + 1; };
  const markSeen = (j) => { seen.add(j.id); seen.add(dedupeKey(j)); };
  const onTopic = allJobs.filter(matchesKeywords).filter(j => !j.postedAt || j.postedAt >= CUTOFF);

  // Cada vaga é decidida uma vez: depois disso fica no seen (menos quem ainda espera o texto).
  const fresh = [], keys = new Set();
  for (const j of onTopic) {
    const key = dedupeKey(j);
    if (seen.has(j.id) || seen.has(key) || keys.has(key)) continue;
    keys.add(key);
    if (APPLIED.test(j.company || '')) { count('applied'); markSeen(j); continue; }
    fresh.push(j);
  }
  // Quem esperava texto e não veio na busca desta rodada (rodízio de buscas, LinkedIn em pausa) volta pela fila.
  let retried = 0;
  for (const [id, p] of Object.entries(pending)) {
    const j = p.job;
    if (!j || seen.has(id) || fresh.some(f => f.id === id)) continue;
    const key = dedupeKey(j);
    if (seen.has(key) || keys.has(key)) { delete pending[id]; continue; }
    keys.add(key);
    fresh.push(j);
    retried++;
  }

  // Regras duras, uma avaliação por vaga. Sem texto (o LinkedIn chega sem) ou com o texto cortado em 2500 pelo
  // coletor antigo: busca a página da vaga antes de decidir. Nunca descarta só por falta de texto.
  const li = { used: 0, blocked: false };
  const newJobs = [], noText = [];
  let waiting = 0;
  for (let j of fresh) {
    j = canon(j);
    let a = F.assess(j);
    let deferred = false;
    const first = a.exclude && a.exclude.key;
    if (first === 'needs_description' || (first === 'no_remote' && j.description.length <= 2500)) {
      const r = await withText(j, li);
      j = r.job;
      deferred = !!r.deferred;
      a = F.assess(j);
    }
    const key = a.exclude && a.exclude.key;
    const tagged = { labels: a.labels, contractor: !!a.contractor.type };
    if (key === 'needs_description') {
      const p = pending[j.id] || { tries: 0, firstAt: Date.now() };
      pending[j.id] = { tries: p.tries + (deferred ? 0 : 1), firstAt: p.firstAt, lastAt: Date.now(), job: pendingSnapshot(j) };
      const waited = Date.now() - pending[j.id].firstAt > MAX_AGE_DAYS * 24 * 3600 * 1000;
      if (pending[j.id].tries < MAX_TEXT_TRIES && !waited) { waiting++; continue; }
      delete pending[j.id];
      count(key);
      markSeen(j);
      noText.push({ ...j, ...tagged, needsDescription: true });
      continue;
    }
    delete pending[j.id];
    markSeen(j);
    if (key) {
      count(key);
      console.error(`   ✗ ${j.title} @ ${j.company}: ${a.exclude.label} (${a.exclude.evidence.slice(0, 140)})`);
      continue;
    }
    newJobs.push({ ...j, ...tagged });
  }

  // Agregador → tenta achar a mesma vaga no ATS da empresa (link direto pra aplicar). Texto diferente lá: decide de novo.
  for (let i = newJobs.length - 1; i >= 0; i--) {
    let j = newJobs[i];
    if (!j.ats) {
      const c = canon(await resolveDirect(j));
      if (c.description !== j.description) {
        const a = F.assess(c);
        if (a.exclude && a.exclude.key !== 'needs_description') {
          count(a.exclude.key);
          console.error(`   ✗ ${j.title} @ ${j.company} (texto do ATS): ${a.exclude.label} (${a.exclude.evidence.slice(0, 140)})`);
          newJobs.splice(i, 1);
          continue;
        }
        j = a.exclude ? { ...c, description: j.description } : { ...c, labels: a.labels, contractor: !!a.contractor.type };
      } else j = c;
    }
    j.fit = fitReasons(j, stripHtml);
    j.people = hotPeople(j, stripHtml(j.description), j.fit);
    newJobs[i] = j;
  }

  console.error(`   Total relevante: ${onTopic.length} | Inéditas: ${fresh.length} | Novas: ${newJobs.length}`);

  // Pool ranqueado pro engagement queue. Mantido mesmo quando não há vaga nova:
  // em dia parado o linkedin-agent ainda precisa de alvos das vagas recentes.
  const ranked = rankJobs(newJobs, stripHtml, { limit: ENGAGE_PER_RUN, perCompany: 1 });
  const unread = rankJobs(noText, stripHtml, { limit: 20, perCompany: 1 });
  status.pool = writeEngagePool([...ranked, ...unread]);
  saveSeen(seen);
  savePending(pending);

  if (newJobs.length > 0) {
    appendLog(newJobs);

    const reportPath = writeReport(newJobs);
    const summary = newJobs.slice(0, 3)
      .map(j => `${j.title} @ ${j.company}`)
      .join(' · ');

    notifyMac(
      `${newJobs.length} nova(s) vaga(s) encontrada(s)`,
      `Toque para abrir: ${summary.substring(0, 150)}`,
      reportPath
    );

    await sendTelegram(newJobs);
    await sendWhatsApp(newJobs);

    // Envia pra email também (pra quando estiver longe do Mac)
    const tempFile = path.join(__dirname, 'data', '.temp-jobs.json');
    fs.writeFileSync(tempFile, JSON.stringify(newJobs));
    try {
      // mailto abria um rascunho a cada hora; só roda se ativado no .env
      if (process.env.JOB_MONITOR_EMAIL === '1') {
        execFileSync('node', [path.join(__dirname, 'send-jobs.js'), tempFile], { stdio: 'inherit' });
      }
    } catch (_) {
      // send-jobs falhou, mas não é crítico — a notificação na tela já avisou
    }
    fs.unlinkSync(tempFile);

    console.log(`\n✅ ${newJobs.length} vagas novas:\n`);
    newJobs.forEach(j => {
      console.log(`• ${j.title} — ${j.company} (${j.ats ? 'direto ' + j.ats : j.source})`);
      console.log(`  ${j.location} | ${j.url}\n`);
    });
  } else {
    console.error('   Nenhuma vaga nova desde a última checagem.');
  }

  Object.assign(liStatus, { errors: [...linkedInStats.errors], requests: linkedInStats.requests, http429: linkedInStats.http429,
    details: linkedInStats.details, blocked: linkedInStats.blocked, skipped: linkedInStats.skipped || '' });
  status.counts = { collected: allJobs.length, onTopic: onTopic.length, fresh: fresh.length, retried, new: newJobs.length,
    toPool: ranked.length, noText: unread.length, waitingText: waiting };
  const out = Object.entries(excluded).sort((x, y) => y[1] - x[1]).map(([k, n]) => `${ruleLabel(k)} ${n}`).join(', ');
  console.error(`   Resumo: ${allJobs.length} coletadas · ${onTopic.length} no tema · ${fresh.length} inéditas`
    + `${retried ? ` (${retried} da fila sem texto)` : ''} · `
    + `${newJobs.length} passaram (${ranked.length} no pool) · fora: ${out || 'nenhuma'} · ${waiting} esperando texto`);
}

async function main() {
  console.error(`🔍 Buscando vagas... (${new Date().toLocaleString('pt-BR')})`);
  const status = { startedAt: new Date().toISOString(), finishedAt: null, ok: false, error: null, sources: [], counts: {}, excluded: {}, pool: null };
  try {
    await collect(status);
    status.ok = true;
  } catch (err) {
    status.error = err.message;
    throw err;
  } finally {
    status.finishedAt = new Date().toISOString();
    try { writeStatus(status); } catch (e) { console.error(`collect-status.json: ${e.message}`); }
  }
}

main().catch(err => {
  console.error('Erro fatal:', err.message);
  process.exit(1);
});
