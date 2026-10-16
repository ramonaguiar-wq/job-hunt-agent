'use strict';
// test-criteria.js - Testes das regras duras de vaga (revisão 2026-10-16). Uso: node test-criteria.js [-v]
// As regras moram em job-criteria.json e rodam em dois motores: filters.js (coleta, pool) e agent/criteria.py (painel).
//  1. Todo padrão do JSON em minúsculas (os motores rodam sem flag de caixa).
//  2. Um caso por regra, os 17 casos do antigo test-remote-filter.js e o ACOOM (reconstruído: a vaga não está em nenhum dado).
//  3. Casos reais congelados em test-fixtures/criteria-real.json, incluindo as 18 rejeições feitas no painel.
//  4. Paridade: cada caso, o pool inteiro, o cache de descrições e textos difíceis de normalizar dão o mesmo resultado nos dois.
//  5. Regra aprendida, recarga do JSON, JSON quebrado e chave fora de "order", num JSON temporário.
const fs = require('fs');
const os = require('os');
const path = require('path');
const util = require('util');
const { execFileSync } = require('child_process');
const F = require('./filters');

const VERBOSE = process.argv.includes('-v');
const PY = process.env.PYTHON || (fs.existsSync('/opt/homebrew/bin/python3') ? '/opt/homebrew/bin/python3' : 'python3');
const CRITERIA_PY = path.join(__dirname, '..', 'agent', 'criteria.py');
let failures = 0;
const fail = (msg) => { failures++; console.log(`FAIL  ${msg}`); };
const pass = (msg) => { if (VERBOSE) console.log(`ok    ${msg}`); };
const section = (t) => console.log(`\n## ${t}`);
const plain = (x) => JSON.parse(JSON.stringify(x === undefined ? null : x));

function runPy(args, input, env = {}) {
  try {
    return execFileSync(PY, ['-W', 'error', ...args], {
      input: input === undefined ? undefined : JSON.stringify(input), maxBuffer: 1 << 28, stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, JOB_CRITERIA: F.RULES_FILE, ...env },
    }).toString();
  } catch (e) {
    fail(`python ${args.join(' ')}: ${(e.stderr || e.message).toString().trim().split('\n').slice(-3).join(' | ')}`);
    return null;
  }
}
const pyCli = (cmd, list) => { const out = runPy([CRITERIA_PY, cmd], list); return out === null ? null : JSON.parse(out); };

// ---- 1. Padrões em minúsculas ----
section('padrões');
const upper = F.criteria().sources.filter((s) => /[A-Z]/.test(s.replace(/\\./g, '')));
if (upper.length) upper.forEach((s) => fail(`padrão com maiúscula (nunca casa, o texto é minúsculo): ${s.slice(0, 120)}`));
else console.log(`${F.criteria().sources.length} padrões compilados, todos em minúsculas`);

// ---- 2. Casos por regra ----
// Texto neutro: inglês, sem remoto, local, moeda, contrato ou idioma. Leva cada caso acima dos 200 caracteres.
const FILL = ' We build software for small businesses. You will partner with product managers and engineers, talk to our customers,'
  + ' and turn what you learn into clear flows, prototypes and polished interfaces that ship every week.';
const REMOTE = 'This is a fully remote position.';
const LOC = JSON.parse(fs.readFileSync(path.join(__dirname, 'test-fixtures', 'atsloc-20261016.json'), 'utf8'));
const DIS = JSON.parse(fs.readFileSync(path.join(__dirname, 'test-fixtures', 'distrib-20261016.json'), 'utf8'));
const ATS = JSON.parse(fs.readFileSync(path.join(__dirname, 'test-fixtures', 'ats-20261016.json'), 'utf8'));
const job = (description, o = {}) => ({ title: 'Senior Product Designer', company: 'Acme', location: 'European Union', description: description + FILL, ...o });
const IT = 'Cerchiamo un product designer per il nostro team. Il lavoro è 100% remoto e la persona lavorerà con il team di prodotto e con gli'
  + ' sviluppatori per progettare le nuove funzionalità della nostra piattaforma. Sarà responsabile della ricerca con gli utenti, dei'
  + ' prototipi e delle interfacce, e collaborerà con il team per migliorare la qualità del prodotto.';
const PT = 'Procuramos uma pessoa designer de produto para a nossa equipe. O trabalho é 100% remoto e você vai trabalhar com a equipe de'
  + ' produto e com os desenvolvedores para criar as novas funcionalidades da nossa plataforma, da pesquisa com os usuários aos'
  + ' protótipos e às interfaces finais, com foco na experiência e na qualidade do produto.';

// [nome, vaga, esperado (chave da regra; null = passa), checagens extras]
const RULE_CASES = [
  ['fechada', job(REMOTE, { closed: true }), 'closed'],
  ['Lead no título', job(REMOTE, { title: 'Lead Product Designer' }), 'seniority'],
  ['Design Lead no fim do título', job(REMOTE, { title: 'Senior Product Designer, Design Lead' }), 'seniority'],
  ['Head of Design', job(REMOTE, { title: 'Head of Design' }), 'seniority'],
  ['Design Manager', job(REMOTE, { title: 'Product Design Manager' }), 'seniority'],
  ['Director', job(REMOTE, { title: 'Director of Product Design' }), 'seniority'],
  ['Principal', job(REMOTE, { title: 'Principal Product Designer' }), 'seniority'],
  ['Junior', job(REMOTE, { title: 'Junior Product Designer' }), 'seniority'],
  ['estágio pelo nível do LinkedIn', job(REMOTE, { title: 'Product Designer', seniority: 'Internship' }), 'seniority'],
  ['Staff sai', job(REMOTE, { title: 'Staff Product Designer' }), 'seniority'],
  ['Mid-Senior passa', job(REMOTE, { title: 'Product Designer (Mid-Senior)' }), null],
  ['"Entry level" do LinkedIn não exclui', job(REMOTE, { seniority: 'Entry level' }), null],
  ['"Ads Manager" é produto, não cargo', job(REMOTE, { title: 'Senior Product Designer, Ads Manager' }), null],
  ['"Lead Generation" é produto, não cargo', job(REMOTE, { title: 'Senior Product Designer, Lead Generation' }), null],

  ['LATAM no título', job(REMOTE, { title: 'Product Designer (LATAM, Remote)' }), 'country'],
  ['Canadá no local', job(REMOTE, { location: 'Toronto, Ontario, Canada' }), 'country'],
  ['Canadá ou Europa no título passa', job(REMOTE, { title: 'Product Designer (Canada or Europe)' }), null],
  ['US-only no texto', job('Remote, US-only.'), 'country'],
  ['"remote, but within the UK"', job('Fully remote, but within the UK.'), 'country'],
  ['"remote in the UK or anywhere in Europe" passa', job('Fully remote in the UK or anywhere in Europe.'), null],
  ['Himalayas com local United States', job(REMOTE, { source: 'himalayas', location: 'United States' }), 'country'],
  ['Himalayas com local Europe passa', job(REMOTE, { source: 'himalayas', location: 'Europe' }), null],
  ['LinkedIn com local United States e texto remoto passa', job(REMOTE, { source: 'linkedin', location: 'United States' }), null],

  ['híbrido 3 dias no escritório', job('Hybrid, 3 days a week in the office.'), 'onsite'],
  ['4 dias por mês no escritório', job('We are a remote team and meet 4 days per month at the office.'), 'onsite'],
  ['papel híbrido', job('This is a hybrid role based in our Paris office.'), 'onsite'],
  ['híbrido ou remoto passa', job('Hybrid or remote, your choice.'), null, { how: 'opção remota' }],
  ['academia on-site é benefício', job(`${REMOTE} Benefits: on-site gym and free lunch at our hubs.`), null],
  ['entrevista on-site não é presencial', job('We are a remote team. Final interview on-site.'), null],
  ['"hybrid intelligence" não é híbrido', job('We are a remote team building hybrid intelligence tools.'), null],
  ['"remote friendly" confirma, mas não anula híbrido', job('Remote friendly culture. Hybrid, 2 days in office.'), 'onsite'],

  ['relocation pedida', job(`${REMOTE} Relocation support to Amsterdam is provided.`), 'relocation'],
  ['relocation como alternativa passa', job(`${REMOTE} We can also discuss relocation to Amsterdam.`), null],
  ['fuso "within 4 hours of Dubai time" passa', job(`${REMOTE} You should be based within 4 hours of Dubai time.`), null],
  ['chefe "based in London" não é relocation', job(`${REMOTE} You will report to our design lead, based in London.`), null],
  ['candidatos nos EUA e Canadá', job('This is a remote position. We are looking for candidates in the US and Canada.'), 'relocation'],
  ['candidatos na Europa passa', job(`${REMOTE} Open to candidates located in Europe.`), null],

  ['autorizado a trabalhar nos EUA', job(`${REMOTE} You must be authorized to work in the United States.`), 'authorization'],
  ['right to work in the UK', job(`${REMOTE} You must have the right to work in the UK.`), 'authorization'],
  ['autorização no país onde mora passa', job(`${REMOTE} You must be authorized to work in your country of residence.`), null],
  ['"no visa sponsorship" sozinho passa', job(`${REMOTE} We are unable to offer visa sponsorship.`), null],

  ['texto curto', job('', { description: 'Fully remote.' }), 'needs_description'],
  ['texto curto, mas final no ATS', job('', { description: 'Fully remote role. Design our app.', descriptionFinal: true }), null],

  ['Madri sem EOR', job(REMOTE, { location: 'Madrid, Community of Madrid, Spain' }), 'spain_local', { country: 'Spain' }],
  ['Espanha com contrato B2B', job(`${REMOTE} This is a B2B contract.`, { location: 'Spain' }), null, { labels: ['B2B', 'moeda?'] }],
  ['Espanha: tipo Contract do LinkedIn não basta', job(REMOTE, { location: 'Spain', employmentType: 'Contract' }), 'spain_local', { labels: ['Contractor', 'moeda?'] }],
  ['Remote Spain no título', job(REMOTE, { title: 'Senior Product Designer (Remote Spain)' }), 'spain_local'],
  ['prefixo "Remote Spain ·" do coletor sai, Valência fica', job(REMOTE, { location: 'Remote Spain · Valencia, Valencian Community, Spain' }), 'spain_local', { country: 'Spain' }],
  ['ACOOM (reconstruída): híbrida em Madri, marcada remota', job('Hybrid work model: 3 days per week at our Madrid office.', { company: 'ACOOM', location: 'Remote Spain · Madrid, Community of Madrid, Spain' }), 'onsite'],

  ['prefixo "Remote European Union ·" não prova remoto', job('Great product job.', { location: 'Remote European Union · Budapest, Hungary' }), 'no_remote', { country: 'Hungary' }],
  ['local "Remote" da própria fonte conta', job('Great product job.', { location: 'Remote' }), null, { how: 'título/local' }],
  ['remoto pela cultura', job('We are a remote-first company.'), null, { how: 'cultura remota' }],
  ['remoto pelo texto', job('You will be working remotely with the team.'), null, { how: 'texto' }],

  ['anúncio em italiano', { title: 'Product Designer', company: 'Acme', location: 'Milano, Lombardy, Italy', description: IT }, 'language', { lang: 'it', country: 'Italy' }],
  ['espanhol fluente exigido passa (the candidate aceita)', job(`${REMOTE} Fluent Spanish is required.`), null],
  ['alemão C1 exigido', job(`${REMOTE} German (C1) required.`), 'language'],
  ['espanhol como plus passa', job(`${REMOTE} Spanish is a plus.`), null],
  ['"Spanish market" não é idioma exigido', job(`${REMOTE} Strong knowledge of the Spanish market.`), null],
  ['português em vaga de Portugal passa', { title: 'Product Designer', company: 'Acme', location: 'Lisboa, Portugal', description: PT }, null, { lang: 'pt' }],
  ['anúncio em português não reprova (CRITERIOS_VAGAS.md)', { title: 'Product Designer', company: 'Acme', location: 'European Union', description: PT }, null, { lang: 'pt', notReason: 'language' }],

  // Idioma exigido (revisão 2026-10-16, caso lemlist): cada frase abaixo exclui por 'language'.
  ...[
    'Fluency in English & French', 'English and French', 'Fluent in English and German', 'French (fluent)', 'Native French',
    'Native-level German', 'Francophone', 'German speaker', 'German-speaking', 'C1 Dutch', 'Italian is required', 'Proficiency in French',
    'Fluency in English and Japanese', 'Mandarin Chinese fluency required', 'Fluent in Polish', 'Swedish (native)', 'Business-level Danish',
    'Norwegian is a must', 'Finnish fluency', 'Fluent in English, Dutch', 'Must speak French', 'Excellent command of English and German',
  ].map((t) => [`idioma exigido: ${t}`, job(`${REMOTE}\n- ${t}\n`), 'language']),
  // Opcional não exclui.
  ...[
    'French is a plus', 'Nice to have: German', 'Bonus if you speak Dutch', 'Ideally French', 'German preferred', 'Italian is advantageous',
    'French would be a nice-to-have',
  ].map((t) => [`idioma opcional: ${t}`, job(`${REMOTE}\n- ${t}\n`), null]),
  // Inglês, espanhol e português exigidos passam.
  ...[
    'English & Spanish', 'Portuguese (native)', 'Fluent in English and Spanish', 'Spanish speaker', 'Spanish-speaking',
    'Native Portuguese', 'Fluency in English & Portuguese', 'Fluent in English',
  ].map((t) => [`idioma liberado: ${t}`, job(`${REMOTE}\n- ${t}\n`), null]),
  // Texto do lemlist (Senior Product Designer, Ashby): francês exigido e "remote friendly" sem frase remota. Sai por idioma e, sem o idioma, por no_remote.
  ['lemlist: inglês e francês + remote friendly', job('About the role\nWe are a growing SaaS company building sales tools.\nWhat we expect\n- Fluency in English & French\n- 5+ years of product design experience\nWhat we offer\n- A remote friendly environment\n- Team events in Paris', { location: 'European Union' }), '*'],
  ['lemlist sem a linha de idioma: só "remote friendly" não confirma remoto', job('About the role\nWe are a growing SaaS company building sales tools.\nWhat we offer\n- A remote friendly environment\n- Team events', { location: 'European Union' }), null, { how: '', hasLabel: 'confirmar: remote friendly' }],
  ['"remote-friendly culture" não confirma', job('Great product job. Remote-friendly culture.'), null, { hasLabel: 'confirmar: remote friendly' }],
  ['"remote friendly company" não confirma', job('Great product job. We are a remote friendly company.'), null, { hasLabel: 'confirmar: remote friendly' }],
  ['"remote friendly" + "fully remote": confirmado, sem label confirmar', job('Great product job. We are a remote friendly company. This role is fully remote.'), null, { noLabel: 'confirmar: remote friendly' }],
  ['sem nenhuma menção a remoto continua no_remote', job('Great product job. Join our team.'), 'no_remote'],
  ['Principal sai (decisão 16/10)', { title: 'Principal Product Designer', company: 'Acme', location: 'Remote', description: 'We are hiring a principal product designer. This role is fully remote across Europe. You will own the design system and work with product and engineering teams on our B2B SaaS platform every day. Salary in EUR.'.repeat(3) }, 'seniority'],
  ['Staff sai (decisão 16/10)', { title: 'Staff Product Designer', company: 'Acme', location: 'Remote', description: 'We are hiring a staff product designer. This role is fully remote across Europe. You will own the design system and work with product and engineering teams on our B2B SaaS platform every day. Salary in EUR.'.repeat(3) }, 'seniority'],
  ['Staff-Level / L6 no título sai', job(REMOTE, { title: 'Product Designer, Generative Media (Staff-Level / L6)' }), 'seniority'],
  ['Senior continua passando', job(REMOTE, { title: 'Senior Product Designer' }), null],
  ['sem frase remota (só escritório) sai', job('Join our team at our Berlin office. We value collaboration.'), 'no_remote'],
  // 16/10 atsloc: local declarado pelo ATS conta como frase remota
  ['Defuse: Greenhouse "Global - Remote" → passa', LOC.defuse, null, { how: 'ATS' }],
  ['Defuse sem atsLocation → no_remote', { ...LOC.defuse, atsLocation: undefined }, 'no_remote'],
  ['atsLocation "Remote - EMEA" confirma', job('We build tools.', { atsLocation: 'Remote - EMEA' }), null, { how: 'ATS' }],
  ['atsWorkplace remote confirma', job('We build tools.', { atsWorkplace: 'remote' }), null, { how: 'ATS' }],
  ['atsLocation remoto não anula ats_hybrid', job('We build tools.', { atsLocation: 'Remote - Europe', atsWorkplace: 'hybrid' }), 'ats_hybrid'],
  ['atsLocation "Remote - US" não confirma', job('We build tools.', { atsLocation: 'Remote - US' }), 'no_remote'],
  ['atsLocation "Hybrid - San Francisco" não confirma', job('We build tools.', { atsLocation: 'Hybrid - San Francisco, California' }), 'no_remote'],
  ['atsLocation remoto não anula spain_local', job('We build tools.', { atsLocation: 'Remote - Spain', location: 'Madrid, Spain' }), 'spain_local'],
  ['LinkedIn: Remote só no título continua no_remote mesmo sem atsLocation', job('We build tools.', { id: 'linkedin-1', title: 'Senior Product Designer (Remote)' }), 'no_remote'],
  // 16/10 distrib: 'distributed team' não confirma remoto; EOR só em contexto de contratação
  ['Waters: só "distributed team" → não confirma remoto, sai', DIS.waters, '*', { hitsInclude: 'no_remote' }],
  ['Deel: "globally distributed companies" + empresa que vende EOR → sai, sem etiqueta EOR', DIS.deel, '*', { hitsInclude: 'no_remote', noLabel: 'EOR' }],
  ['Defuse: só "globally distributed team" no corpo → no_remote', DIS.defuse, 'no_remote'],
  ['"distributed team" sozinho não confirma', job('We are a distributed team across many timezones.'), 'no_remote'],
  ['"we hire through an EOR" → etiqueta EOR', job('This role is fully remote. We hire through an EOR in your country.'), null, { hasLabel: 'EOR' }],
  ['"employment via Deel" → etiqueta EOR', job('Fully remote role. Employment is via Deel for candidates outside the US.'), null, { hasLabel: 'EOR' }],
  ['empresa que vende EOR (Deel is the payroll platform) → sem etiqueta EOR', job('Fully remote role. Deel is the all-in-one payroll and HR platform; we offer employer of record services to clients.'), null, { noLabel: 'EOR' }],
  ['Espanha + "we hire via Remote.com" → não é spain_local', job('This role is fully remote. We hire via Remote.com as employer of record.', { location: 'Madrid, Spain' }), null, { notReason: 'spain_local' }],
  ['Espanha + só menciona EOR como produto → spain_local', job('This role is fully remote. Our product helps companies with employer of record compliance.', { location: 'Madrid, Spain' }), 'spain_local'],
  // Vagas reais de 16/10 (test-fixtures/ats-20261016.json)
  ['Lendable: ATS Ashby diz Hybrid → ats_hybrid', ATS.lendable, 'ats_hybrid'],
  ['Lendable sem atsWorkplace: continua confirmar (remoto genérico)', { ...ATS.lendable, atsWorkplace: undefined }, null, { hasLabel: 'confirmar: remoto genérico' }],
  ['Ladders: ATS Lever onsite → ats_onsite, e Remote só no título → no_remote', ATS.ladders, 'ats_onsite', { hitsInclude: 'no_remote' }],
  ['Ladders sem atsWorkplace: Remote só no título do LinkedIn → no_remote', { ...ATS.ladders, atsWorkplace: undefined }, 'no_remote'],
  ['Keen: freelance para empresa de Barcelona → confirmar', ATS.keen, null, { hasLabel: 'confirmar: freelance empresa espanhola' }],
  ['Salve.Lab: B2B/contractor sem base espanhola → passa limpo', ATS.salve, null, { noLabel: 'confirmar: freelance empresa espanhola' }],
  ['freelance + empresa em Madrid mas via EOR → sem label', job('We are based in Madrid. Freelance or contractor via Deel EOR. This role is fully remote.'), null, { noLabel: 'confirmar: freelance empresa espanhola' }],
  ['vaga não-LinkedIn com local Remote continua confirmada pelo local', { title: 'Senior Product Designer', company: 'Acme', location: 'Remote', source: 'dribbble', description: 'We build B2B SaaS tools for teams and need a senior product designer to own our design system end to end with product and engineering. '.repeat(3) }, null],
  ['Lead continua saindo', { title: 'Lead Product Designer', company: 'Acme', location: 'Remote', description: 'This role is fully remote. '.repeat(30) }, 'seniority'],
  ['Junior continua saindo', { title: 'Junior Product Designer', company: 'Acme', location: 'Remote', description: 'This role is fully remote. '.repeat(30) }, 'seniority'],
  ['Lendable: benefício genérico hybrid/fully remote roles vira confirmar', { title: 'Senior Product Designer', company: 'Lendable', location: 'London, England, United Kingdom', description: 'We are building fintech products for millions of customers and need a senior product designer to own our lending journeys. You will work with product managers and engineers. Interview process: final round in person at the office. Flexible working: flexible approach tailored to each role. Hybrid roles require three days in-office weekly; fully remote roles include regular opportunities for in-person connection through socials and off-sites. Health coverage and private health cover.' }, null, { hasLabel: 'confirmar: remoto genérico' }],
  ['fully remote explícito sem hybrid roles continua confirmado', job('Great product job. This is a fully remote role across Europe.'), null, { noLabel: 'confirmar: remoto genérico' }],
  ['"remote friendly" + "fully remote" confirma', job('A remote friendly environment. This is a fully remote role.'), null, { how: 'opção remota' }],
  ['"remote friendly" + work from anywhere confirma', job('Remote friendly culture. You can work from anywhere in Europe.'), null],
  ['"remote friendly" + remote-first confirma', job('A remote friendly environment. We are remote-first.'), null],
  ['"remote friendly" com local Remote confirma pelo local', job('A remote friendly environment.', { location: 'Remote' }), null, { how: 'título/local' }],

  ['salário em PLN', job(`${REMOTE} Salary: 15 000 - 20 000 PLN per month.`), 'currency', { currency: 'bad' }],
  ['salário em R$', job(`${REMOTE} Salary: R$ 12.000 per month.`), 'currency', { currency: 'bad' }],
  ['salário em EUR', job(`${REMOTE} Salary: €60,000 - €80,000.`), null, { currency: 'ok', labels: [] }],
  ['salário em USD', job(`${REMOTE} Compensation: $120k - $150k.`), null, { currency: 'ok' }],
  ['PLN ou EUR: EUR vale', job(`${REMOTE} Salary: 15 000 PLN or 3 500 EUR per month.`), null, { currency: 'ok' }],
  ['sem moeda vira etiqueta "moeda?"', job(REMOTE), null, { currency: 'unknown', labels: ['moeda?'] }],

  ['etiqueta EOR', job(`${REMOTE} We hire through Deel as employer of record.`), null, { contractor: 'EOR' }],
  ['etiqueta B2B', job(`${REMOTE} Cooperation on a B2B basis.`), null, { contractor: 'B2B' }],
  ['"B2B SaaS" não é contrato', job(`${REMOTE} Experience with B2B SaaS products.`), null, { contractor: null }],
  ['etiqueta Contractor', job(`${REMOTE} This is an independent contractor role.`), null, { contractor: 'Contractor' }],
  ['tipo Contract do LinkedIn vira Contractor', job(REMOTE, { employmentType: 'Contract' }), null, { contractor: 'Contractor' }],
];

// Os 17 casos do antigo test-remote-filter.js. O texto de enchimento antigo tinha 105 caracteres e hoje cairia em
// needs_description (mínimo 200), por isso usam o FILL. Mudou o esperado: "sem descrição, Remote Spain" agora pede o texto.
const MIGRATED = [
  ['tagged remote + hybrid 3 days office', { remote: true, location: 'Remote', description: `Hybrid, 3 days in office.${FILL}` }, 'onsite'],
  ['fully remote, work from anywhere', { remote: true, location: 'Remote', description: `Fully remote, work from anywhere.${FILL}` }, null],
  ['tagged remote, no remote text', { remote: true, location: 'Lisbon', description: `Great product job.${FILL}` }, 'no_remote'],
  ['Remote Spain via Deel EOR', { remote: true, location: 'Remote, Spain', description: `Fully remote, hired via Deel EOR.${FILL}` }, null, { labels: ['EOR', 'moeda?'] }],
  ['relocation to Berlin provided', { remote: true, location: 'Remote', description: `Remote role, relocation to Berlin provided.${FILL}` }, 'relocation'],
  ['no relocation required, fully remote', { remote: true, location: 'Remote', description: `No relocation required, fully remote.${FILL}` }, null],
  ['must be located in London', { remote: true, location: 'Remote', description: `Remote team, must be located in London.${FILL}` }, 'relocation'],
  ['not hybrid, remote', { location: 'Remote', description: `This is not hybrid, 100% remote.${FILL}` }, null],
  ['title onsite despite Remote location', { title: 'Engineer (On-site)', location: 'Remote', description: `Remote friendly.${FILL}` }, 'onsite'],
  ['based in Paris', { remote: true, location: 'Remote', description: `Remote, based in Paris.${FILL}` }, 'relocation'],
  ['presencial (es)', { remote: true, location: 'Remote', description: `Trabajo remoto y presencial.${FILL}` }, 'onsite'],
  ['remote (no relocation)', { location: 'Remote (no relocation)', description: `Work from home.${FILL}` }, null],
  ['empty desc, location Remote Spain', { location: 'Remote Spain', description: '' }, 'needs_description'],
  ['empty desc, Lisbon, no remote signal', { title: 'Product Designer', location: 'Lisbon', description: '' }, 'needs_description'],
  ['empty desc, Hybrid in title', { title: 'UX Designer (Hybrid)', location: 'Barcelona', description: '' }, 'onsite'],
  ['desc without remote words, location without remote', { title: 'Product Designer', location: 'Lisbon', description: `Great product job.${FILL}` }, 'no_remote'],
  ['based in GMT timezones is not relocation', { location: 'Remote', description: `Remote, based in GMT-3 to GMT+3 timezones.${FILL}` }, null],
];

// expect: chave da regra que exclui; null = passa; '*' = sai por qualquer regra que não seja só falta de texto.
function check(name, j, expect, more = {}) {
  const a = F.assess(j);
  const got = a.exclude ? a.exclude.key : null;
  const errs = [];
  const okKey = expect === '*' ? !!got && got !== 'needs_description' : got === expect;
  if (!okKey) errs.push(`esperado ${expect || 'passa'}, deu ${got || 'passa'}${a.exclude ? ` (${a.exclude.evidence})` : ''}`);
  if (more.labels && !util.isDeepStrictEqual(a.labels, more.labels)) errs.push(`etiquetas ${JSON.stringify(a.labels)}, esperado ${JSON.stringify(more.labels)}`);
  if (more.hasLabel && !a.labels.includes(more.hasLabel)) errs.push(`falta etiqueta ${more.hasLabel} em ${JSON.stringify(a.labels)}`);
  if (more.noLabel && a.labels.includes(more.noLabel)) errs.push(`etiqueta indevida ${more.noLabel}`);
  if (more.hitsInclude && !(more.hitsInclude in a.hits)) errs.push(`falta hit ${more.hitsInclude} em ${Object.keys(a.hits)}`);
  if (more.notReason && more.notReason in a.hits) errs.push(`hit indevido ${more.notReason}: ${a.hits[more.notReason]}`);
  if (more.how !== undefined && a.remote.how !== more.how) errs.push(`remoto por "${a.remote.how}", esperado "${more.how}"`);
  if (more.currency && a.currency.status !== more.currency) errs.push(`moeda ${a.currency.status}, esperado ${more.currency}`);
  if (more.contractor !== undefined && a.contractor.type !== more.contractor) errs.push(`contrato ${a.contractor.type}, esperado ${more.contractor}`);
  if (more.country !== undefined && a.country !== more.country) errs.push(`país ${a.country}, esperado ${more.country}`);
  if (more.lang !== undefined && a.lang !== more.lang) errs.push(`idioma ${a.lang}, esperado ${more.lang}`);
  if (errs.length) fail(`${name}: ${errs.join('; ')}`);
  else pass(`${name} → ${got || 'passa'}`);
  return a;
}

const parityJobs = [];
const runCases = (title, list) => {
  section(title);
  const before = failures;
  for (const [name, j, expect, more] of list) { check(name, j, expect, more); parityJobs.push([`${title}: ${name}`, j]); }
  console.log(`${list.length} casos, ${failures - before} falhas`);
};
runCases('uma regra por caso', RULE_CASES);
runCases('casos migrados do test-remote-filter.js', MIGRATED);

// ---- 3. Casos reais ----
const FX = JSON.parse(fs.readFileSync(path.join(__dirname, 'test-fixtures', 'criteria-real.json'), 'utf8'));
runCases('casos reais (test-fixtures/criteria-real.json)', FX.cases.map((c) => [c.name, c.job, c.expect]));
const rej = FX.cases.filter((c) => c.rejection);
const gone = rej.filter((c) => { const x = F.hardExclude(c.job); return x && x.key !== 'needs_description'; });
console.log(`rejeições do painel: ${gone.length} de ${rej.length} saem por regra dura; `
  + `${rej.filter((c) => (F.hardExclude(c.job) || {}).key === 'needs_description').length} esperam o texto; ficam: `
  + `${rej.filter((c) => !F.hardExclude(c.job)).map((c) => c.job.company).join(', ') || 'nenhuma'}`);

// ---- 4. Paridade JS x Python ----
section('paridade JS x Python');
function poolJobs() {
  const read = (f, d) => { try { return JSON.parse(fs.readFileSync(path.join(__dirname, 'data', f), 'utf8')); } catch { return d; } };
  const pool = read('engage-jobs.json', { jobs: [] });
  const cache = read('description-cache.json', {});
  const jobs = (Array.isArray(pool) ? pool : pool.jobs || []).map((j) => {
    const c = cache[j.id] || {};
    return { ...j, ...(c.description ? { description: c.description } : {}), ...(c.location ? { location: c.location } : {}),
      seniority: c.seniority, employmentType: c.employmentType, closed: c.closed };
  });
  const ids = new Set(jobs.map((j) => j.id));
  for (const [id, c] of Object.entries(cache)) if (!ids.has(id)) jobs.push({ id, ...c });
  return jobs;
}
const cp = (...n) => String.fromCodePoint(...n);
const cu = (...n) => String.fromCharCode(...n);
const TRICKY = [
  '<p>Hello&nbsp;World</p><br/>&amp;lt;b&amp;gt;X &lt;p&gt;escaped&lt;/p&gt;',
  `emoji &#128512; ${cp(0x1f600)} flag ${cp(0x1f1ea, 0x1f1f8)} end`,
  `lone ${cu(0xd800)} high, lone ${cu(0xdc00)} low, par por entidade &#55357;&#56832; fim`,
  `nbsp${cp(0xa0)}x${cp(0x2028)}line${cp(0x2029)}para${cp(0x202f)}narrow${cp(0x3000)}wide`,
  'crlf\r\nline\rcr\n\n\n  spaced \t tab  \n ',
  `${cp(0x130)}stanbul ${cp(0x3a3, 0x399, 0x3a3, 0x3a5, 0x3a6, 0x39f, 0x3a3)} STRASSE ${cp(0x1e9e)} ${cp(0x49)}${cp(0x131)}`,
  `e${cp(0x301)} composto, ${cp(0x212b)} angstrom, ${cp(0xfb01)} ligadura, ${cp(0x2126)} ohm`,
  `soft${cp(0xad)}hyphen zero${cp(0x200b)}width ${cp(0xfeff)}bom ${cp(0x2060)}joiner`,
  '&#x41;&#65;&unknown;&#0;&#1114112;&AMP;&Amp;&#X42;&euro;&pound;&hellip;',
  `${cp(0x2018)}aspas${cp(0x2019)} ${cp(0x201c)}duplas${cp(0x201d)} non${cp(0x2011)}breaking ${cp(0x2010)}hyphen ${cp(0x2bc)}`,
  `ctrl${cu(1)}${cu(0x7f)}${cu(0x85)}${cu(0x0b)}${cu(0x0c)}end`,
  '<div class="x">Block</div><span>inline</span><LI>item<BR>quebra<H2 id=t>titulo</H2>',
  '   ', '', null, 0, 1.5, 2, true, false, ['a', 1, null, ['b', 'c']], { k: 1 },
];
const trickyJobs = TRICKY.map((t, i) => ({ title: i % 2 ? t : `<b>Senior</b> Product Designer ${i}`, company: i % 3 ? null : t, location: i % 2 ? ['Remote', 'Spain'] : t,
  description: `${TRICKY.map(String).join('\n')}${FILL} Fully remote. Salary: €60,000.`, salary: i % 2 ? 5000 : t, tags: ['B2B', 'contract basis'], seniority: i,
  employmentType: ['Contract'], closed: 'true' }));
const sets = [['casos', parityJobs.map(([, j]) => j), parityJobs.map(([n]) => n)], ['pool + cache', poolJobs()], ['vagas com campos estranhos', trickyJobs]];
for (const [title, jobs, names] of sets) {
  const py = pyCli('assess', jobs);
  if (!py) continue;
  let diff = 0;
  jobs.forEach((j, i) => {
    const js = plain(F.assess(j));
    if (util.isDeepStrictEqual(js, py[i])) return;
    diff++;
    const keys = Object.keys(js).filter((k) => !util.isDeepStrictEqual(js[k], py[i][k]));
    fail(`paridade (${title}) ${names ? names[i] : j.id || i}: ${keys.map((k) => `${k} js=${JSON.stringify(js[k])} py=${JSON.stringify(py[i][k])}`).join('; ').slice(0, 600)}`);
  });
  console.log(`${title}: ${jobs.length} vagas, ${diff} diferenças`);
}
const pyNorm = pyCli('normalize', TRICKY);
if (pyNorm) {
  let diff = 0;
  TRICKY.forEach((t, i) => { const js = F.normalize(t); if (js !== pyNorm[i]) { diff++; fail(`normalize ${JSON.stringify(t).slice(0, 60)}: js=${JSON.stringify(js)} py=${JSON.stringify(pyNorm[i])}`); } });
  console.log(`normalize: ${TRICKY.length} textos difíceis, ${diff} diferenças`);
}
// Funções avulsas que o painel e a coleta usam.
const API_IN = { locs: ['Valencia, Valencian Community, Spain', 'Remote European Union · Legnano, Lombardy, Italy', 'Remote', ''], text: IT,
  jobs: [job(REMOTE, { location: 'Spain', employmentType: 'Contract' }), job('', { description: '' }), job(`${REMOTE} B2B contract.`)] };
const apiJs = { country: API_IN.locs.map(F.countryOf), lang: F.detectLang(API_IN.text), reason: API_IN.jobs.map(F.hardExcludeReason),
  needs: API_IN.jobs.map(F.needsDescription), labels: API_IN.jobs.map(F.labelsFor) };
const apiPyOut = runPy(['-c', [
  'import json, sys; sys.path.insert(0, sys.argv[1]); import criteria as K',
  'i = json.load(sys.stdin)',
  "print(json.dumps({'country': [K.country_of(l) for l in i['locs']], 'lang': K.detect_lang(i['text']),",
  "  'reason': [K.hard_exclude_reason(j) for j in i['jobs']], 'needs': [K.needs_description(j) for j in i['jobs']],",
  "  'labels': [K.labels_for(j) for j in i['jobs']]}))"].join('\n'), path.dirname(CRITERIA_PY)], API_IN);
if (apiPyOut !== null) {
  const apiPy = JSON.parse(apiPyOut);
  if (util.isDeepStrictEqual(plain(apiJs), apiPy)) console.log(`funções avulsas iguais: ${JSON.stringify(apiJs.country)} ${apiJs.lang}`);
  else fail(`funções avulsas: js=${JSON.stringify(apiJs)} py=${JSON.stringify(apiPy)}`);
  if (apiJs.country[0] !== 'Spain' || apiJs.country[1] !== 'Italy' || apiJs.lang !== 'it') fail(`countryOf/detectLang: ${JSON.stringify(apiJs)}`);
}
for (const k of ['CONTRACTOR_HINTS', 'EXCLUDE_ONSITE_LOC', 'EXCLUDE_ONSITE_DESC']) if (!(F[k] instanceof RegExp)) fail(`filters.${k} não é RegExp`);

// ---- 5. Regra aprendida, recarga, JSON quebrado e "order" ----
section('regra aprendida e recarga do JSON');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'criteria-test-'));
const tmpRules = path.join(tmpDir, 'job-criteria.json');
fs.copyFileSync(F.RULES_FILE, tmpRules);
const CASE = {
  job: job(REMOTE, { company: 'Acme Agency' }),
  noRemote: job('Great product job.', { company: 'Acme Agency' }),
  learned: { id: 'empresa-acme-agency', field: 'company', label: 'empresa Acme Agency', ui: 'Empresa/setor', re: '\\bacme agency\\b' },
};
const RELOAD_JS = `
const fs = require('fs');
const F = require(process.argv[1]);
const file = process.env.JOB_CRITERIA, c = JSON.parse(process.env.CASE), base = JSON.parse(fs.readFileSync(file, 'utf8'));
const save = (o) => fs.writeFileSync(file, typeof o === 'string' ? o : JSON.stringify(o));
const out = [];
const step = (j) => { const a = F.assess(j); out.push({ exclude: a.exclude, hits: Object.keys(a.hits).sort() }); };
step(c.job);
save({ ...base, learned: [c.learned] }); step(c.job);
save('{ quebrado'); step(c.job);
save({ ...base, order: base.order.filter((k) => k !== 'no_remote') }); step(c.noRemote);
save(base);
console.log(JSON.stringify(out));`;
const RELOAD_PY = `
import json, os, sys
sys.path.insert(0, sys.argv[1])
import criteria as K
f, c = os.environ['JOB_CRITERIA'], json.loads(os.environ['CASE'])
base = json.load(open(f, encoding='utf-8'))
def save(o):
    with open(f, 'w', encoding='utf-8') as h:
        h.write(o if isinstance(o, str) else json.dumps(o))
out = []
def step(j):
    a = K.assess(j)
    out.append({'exclude': a['exclude'], 'hits': sorted(a['hits'])})
step(c['job'])
save({**base, 'learned': [c['learned']]}); step(c['job'])
save('{ quebrado'); step(c['job'])
save({**base, 'order': [k for k in base['order'] if k != 'no_remote']}); step(c['noRemote'])
save(base)
print(json.dumps(out))`;
const env = { ...process.env, JOB_CRITERIA: tmpRules, CASE: JSON.stringify(CASE) };
let reloadJs = null;
try {
  reloadJs = JSON.parse(execFileSync(process.execPath, ['-e', RELOAD_JS, path.join(__dirname, 'filters.js')], { env, stdio: ['pipe', 'pipe', 'pipe'] }).toString());
} catch (e) { fail(`recarga no JS: ${(e.stderr || e.message).toString().trim().split('\n').slice(-2).join(' | ')}`); }
const reloadPyOut = runPy(['-c', RELOAD_PY, path.dirname(CRITERIA_PY)], undefined, { JOB_CRITERIA: tmpRules, CASE: JSON.stringify(CASE) });
fs.rmSync(tmpDir, { recursive: true, force: true });
const reloadPy = reloadPyOut === null ? null : JSON.parse(reloadPyOut);
for (const [eng, out] of [['JS', reloadJs], ['Python', reloadPy]]) {
  if (!out) continue;
  const [plainRun, learned, broken, noOrder] = out;
  if (plainRun.exclude) fail(`${eng}: Acme Agency sem regra aprendida deveria passar, deu ${plainRun.exclude.key}`);
  const want = { key: 'learned', label: 'regra aprendida: empresa Acme Agency', ui: 'Empresa/setor', rule: 'empresa-acme-agency' };
  if (!learned.exclude || Object.keys(want).some((k) => learned.exclude[k] !== want[k])) fail(`${eng}: regra aprendida não excluiu como esperado: ${JSON.stringify(learned.exclude)}`);
  if (!util.isDeepStrictEqual(broken, learned)) fail(`${eng}: com o JSON quebrado deveria manter a última versão boa, deu ${JSON.stringify(broken.exclude)}`);
  if (noOrder.exclude || !noOrder.hits.includes('no_remote')) fail(`${eng}: tirar no_remote de "order" deveria só desligar a exclusão: ${JSON.stringify(noOrder)}`);
}
if (reloadJs && reloadPy) {
  if (util.isDeepStrictEqual(plain(reloadJs), reloadPy)) console.log('JS e Python: regra aprendida exclui, JSON quebrado mantém a versão boa, chave fora de "order" só aparece em hits');
  else fail(`recarga: JS e Python diferentes: js=${JSON.stringify(reloadJs)} py=${JSON.stringify(reloadPy)}`);
}

console.log(`\n${failures ? `${failures} FALHAS` : 'TUDO OK'}`);
process.exit(failures ? 1 : 0);
