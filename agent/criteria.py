#!/usr/bin/env python3
"""criteria.py - Espelho em Python de monitor/filters.js (revisão 2026-10-16).

As regras duras de vaga moram em monitor/job-criteria.json; aqui elas só são aplicadas, do mesmo
jeito que no filters.js. O painel (connect.py) usa este módulo; a coleta e o pool usam o filters.js.
Mudou uma regra? Edite o JSON e rode `node monitor/test-criteria.js`: cada caso roda nos dois.

  assess(job)       -> {exclude, remote, contractor, currency, country, lang, labels, hits}, igual ao JS
  hard_exclude(job) -> {key, label, ui, evidence} da primeira regra (na ordem de "order") que exclui, ou None

CLI (usado pelo teste de paridade): python3 criteria.py assess|normalize < lista.json > resultado.json
Paridade: todo texto passa por normalize() (minúsculas, só ' ' e '\\n' como espaço, nada fora do BMP) e todo
padrão compila só com re.A (no JS, só 'g'), então \\b, \\w, \\s, '.' e os índices batem nos dois.
"""
import json
import os
import re
import sys
import unicodedata

RULES_FILE = os.environ.get('JOB_CRITERIA') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'monitor', 'job-criteria.json')


# ---- Normalização (idêntica a filters.normalize) ----
def as_text(v):
    """Como o asText do JS: None/False viram '', lista vira 'a, b', número inteiro sai sem '.0'."""
    if v is None or v is False:
        return ''
    if v is True:
        return 'true'
    if isinstance(v, (list, tuple)):
        return ', '.join(as_text(x) for x in v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e21 else repr(v)
    if isinstance(v, dict):
        return '[object Object]'
    return str(v)


ENTITIES = {'nbsp': ' ', 'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'", 'rsquo': "'", 'lsquo': "'",
            'ldquo': '"', 'rdquo': '"', 'ndash': '\u2013', 'mdash': '\u2014', 'hellip': '\u2026', 'bull': '\u2022',
            'middot': '\u00b7', 'euro': '\u20ac', 'pound': '\u00a3'}
BLOCK_TAG = re.compile(r'</?(br|p|li|ul|ol|div|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>', re.I | re.A)
ANY_TAG = re.compile(r'<[^>]+>')
ENTITY = re.compile(r'&(#\d+|#x[0-9a-f]+|[a-z]+);', re.I | re.A)
_ASTRAL = re.compile(r'[\U00010000-\U0010ffff]')
_SURROGATE = re.compile(r'[\ud800-\udfff]')
_ZERO_WIDTH = re.compile(r'[\u200b\u200c\u200d\u2060\u00ad]')
_SPACES = re.compile(r'[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]')
_CR = re.compile(r'\r\n?')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
_SQUOTE = re.compile(r'[\u2018\u2019\u02bc]')
_DQUOTE = re.compile(r'[\u201c\u201d]')
_HYPHEN = re.compile(r'[\u2010\u2011]')
_HSPACE = re.compile(r'[ \t]+')
_NEWLINE = re.compile(r' ?\n[ \n]*')
_WORD = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_')


def _decode_entity(m):
    whole, b = m.group(0), m.group(1).lower()
    if b[0] == '#':
        n = int(b[2:], 16) if b[1] == 'x' else int(b[1:], 10)
        return chr(n) if 0 < n <= 0x10FFFF else whole
    return ENTITIES.get(b, whole)


def normalize(s):
    """HTML vira texto (duas passadas: há descrição com HTML escapado duas vezes), emoji e afins viram espaço,
    espaços exóticos viram ' ', aspas curvas viram retas, e tudo fica em minúsculas."""
    t = as_text(s)
    for _ in range(2):
        t = ENTITY.sub(_decode_entity, ANY_TAG.sub(' ', BLOCK_TAG.sub('\n', t)))
    t = unicodedata.normalize('NFC', _SURROGATE.sub(' ', _ASTRAL.sub(' ', t)))
    t = _CONTROL.sub(' ', _CR.sub('\n', _SPACES.sub(' ', _ZERO_WIDTH.sub('', t))))
    t = _HYPHEN.sub('-', _DQUOTE.sub('"', _SQUOTE.sub("'", t)))
    t = _NEWLINE.sub('\n', _HSPACE.sub(' ', t))
    return t.strip(' \n').lower()


# ---- Compilação do JSON ----
def _compile(raw):
    E, R = raw['engine'], raw['rules']
    L, C, T, O = R['language'], R['currency'], R['contractor'], R['onsite']
    sources = []

    def expand(s):
        if s == '@spain':
            return R['spain_local']['places']
        return s.replace('{LANGS}', L['requiredLanguages']).replace('{AMT}', E['amount'])

    def rx(s):
        src = expand(s)
        sources.append(src)
        return re.compile(src, re.A)

    def pat(p):
        if isinstance(p, str):
            return {'re': rx(p), 'unless': [], 'exceptBefore': [], 'exceptAfter': []}
        return {'re': rx(p['re']), 'unless': [rx(s) for s in p.get('unless') or []],
                'exceptBefore': [rx(s) for s in p.get('exceptBefore') or []],
                'exceptAfter': [rx(s) for s in p.get('exceptAfter') or []]}

    def group(patterns, exceptBefore=None, exceptAfter=None, afterChecks=None, noNeg=False):
        return {'pats': [pat(p) for p in patterns], 'exceptBefore': [rx(s) for s in exceptBefore or []],
                'exceptAfter': [rx(s) for s in exceptAfter or []],
                'afterChecks': [(rx(r), w) for r, w in afterChecks or []], 'noNeg': bool(noNeg)}

    def guards(r):
        return {'exceptBefore': r.get('exceptBefore'), 'exceptAfter': r.get('exceptAfter')}

    country, reloc, spain = R['country'], R['relocation'], R['spain_local']
    return {
        'raw': raw, 'sources': sources, 'order': raw['order'], 'R': R,
        'win_neg': E['negationWindow'], 'win_guard': E['guardWindow'], 'win_after': E['afterWindow'], 'win_ctx': E['evidenceContext'],
        'min_desc': E['minDescriptionChars'], 'min_tokens': E['minLangTokens'], 'min_hits': E['minLangHits'],
        'clause_split': rx(E['clauseSplit']), 'guard_split': rx(E['guardSplit']), 'sentence_split': rx(E['sentenceSplit']),
        'token': rx(E['tokenPattern']), 'neg_before': rx(E['negationBefore']), 'neg_ignore': rx(E['negationIgnore']),
        'neg_after': rx(E['negationAfter']), 'synthetic': rx(E['syntheticLocationPrefix']),
        'seniority': group(R['seniority']['patterns']), 'seniority_meta': group(R['seniority']['metaPatterns']),
        'learned': [{'id': as_text(l.get('id')), 'field': l.get('field'), 'label': as_text(l.get('label') or l.get('id')),
                     'ui': l.get('ui') or R['learned']['ui'], 'g': group([l['re']], noNeg=l.get('field') != 'text')}
                    for l in raw.get('learned') or []],
        'country_head': group(country['headPatterns']), 'country_head_unless': rx(country['headUnless']),
        'country_text': group(country['textPatterns']),
        'cand_sources': country['candidateLocation']['sources'], 'cand_allowed': rx(country['candidateLocation']['allowed']),
        'onsite_explicit': group(O['explicit'], O.get('explicitExceptBefore'), O.get('explicitExceptAfter')),
        'onsite_labels': group(O['labels'], O.get('labelsExceptBefore'), O.get('labelsExceptAfter')),
        'relocation': group(reloc['patterns'], afterChecks=[(reloc['placeOk'], E['placeWindow'])]),
        'authorization': group(R['authorization']['patterns'], exceptAfter=R['authorization'].get('exceptAfter')),
        'spain_places': rx(spain['places']), 'spain_head': [rx(s) for s in spain['headPatterns']],
        'spain_foreign': group(spain['foreignOk']),
        'remote_option': group(R['remoteOption']['patterns'], **guards(R['remoteOption'])),
        'remote_culture': group(R['remoteCulture']['patterns'], **guards(R['remoteCulture'])),
        'remote_confirm': group(R['remoteConfirm']['patterns'], **guards(R['remoteConfirm'])),
        'remote_head': group(R['remoteConfirm']['headPatterns'], **guards(R['remoteConfirm'])),
        'remote_friendly': rx(R['no_remote'].get('friendly') or '(?!)'),
        'remote_generic_ev': rx(R['no_remote'].get('genericEvidence') or '(?!)'),
        'remote_generic_ctx': rx(R['no_remote'].get('genericContext') or '(?!)'),
        'seniority_confirm': rx(R['seniority'].get('confirm') or '(?!)'),
        'ats_loc_remote': rx(R['no_remote'].get('atsLocationRemote') or '(?!)'),
        'ats_loc_not': rx(R['no_remote'].get('atsLocationNot') or '(?!)'),
        'spanish_base': rx(R['spain_local'].get('spanishBase') or '(?!)'),
        'foreign_entity': rx(R['spain_local'].get('foreignEntity') or '(?!)'),
        'langs': list(L['stopwords'].keys()),
        'stop': {k: frozenset(normalize(w) for w in words) for k, words in L['stopwords'].items()},
        'lang_allowed': L['allowed'], 'lang_when': {k: rx(r) for k, r in (L.get('allowedWhenPlace') or {}).items()},
        'lang_require': group(L['requirePatterns'], exceptAfter=L.get('requireExceptAfter')),
        'lang_optional': rx(L['optional']), 'lang_local': rx(L['localTokens']),
        'currency_good': group(C['good'], noNeg=True), 'currency_bad': group(C['bad'], noNeg=True),
        'currency_codes': [(k, rx(r)) for k, r in C['codes']],
        'contractor_types': [(k, group([r])) for k, r in T['types']], 'contractor_meta': rx(T['metaContract']),
        'countries': [(k, rx(r)) for k, r in raw['countries']],
    }


# Recarrega quando o JSON muda (mtime/tamanho). JSON quebrado no meio de uma edição: segue com a última versão boa.
_cache = None


def criteria():
    global _cache
    st = os.stat(RULES_FILE)
    key = (st.st_mtime_ns, st.st_size)
    cached = _cache
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(RULES_FILE, encoding='utf-8') as f:
            c = _compile(json.load(f))
    except Exception as e:  # noqa: BLE001 - qualquer erro de JSON/regex mantém a versão boa
        if not cached:
            raise
        print(f'criteria.py: {RULES_FILE} inválido, mantendo a versão anterior: {e}', file=sys.stderr)
        c = cached[1]
    _cache = (key, c)
    return c


# ---- Motor ----
def _has(r, s):
    return r.search(s) is not None


def _last_piece(s, splitter):
    return splitter.split(s)[-1]


# Janelas não cortam palavra ao meio; senão um \b na borda da janela daria resultado diferente do texto inteiro.
def _cut_start(text, s, limit):
    s = max(0, s)
    while 0 < s < limit and text[s - 1] in _WORD and text[s] in _WORD:
        s += 1
    return s


def _cut_end(text, e, start):
    e = min(len(text), e)
    while start < e < len(text) and text[e - 1] in _WORD and text[e] in _WORD:
        e += 1
    return e


# "no relocation", "not hybrid", "relocation is not required"
def _negated(c, text, i, end):
    before = c['neg_ignore'].sub(' ', _last_piece(text[_cut_start(text, i - c['win_neg'], i):i], c['clause_split']))
    return _has(c['neg_before'], before) or _has(c['neg_after'], text[end:_cut_end(text, end + c['win_after'], end)])


def _guarded(c, text, i, end, p, g):
    before = _last_piece(text[_cut_start(text, i - c['win_guard'], i):i], c['guard_split'])
    after = text[end:_cut_end(text, end + c['win_after'], end)]
    if any(_has(r, before) for r in p['exceptBefore']) or any(_has(r, before) for r in g['exceptBefore']):
        return True
    if any(_has(r, after) for r in p['exceptAfter']) or any(_has(r, after) for r in g['exceptAfter']):
        return True
    return any(_has(r, text[end:_cut_end(text, end + w, end)]) for r, w in g['afterChecks'])


def _evidence(c, text, i, end):
    s, e = _cut_start(text, i - c['win_ctx'], i), _cut_end(text, end + c['win_ctx'], end)
    ev = ' / '.join(text[s:e].split('\n')).strip(' ')
    return ('…' if s > 0 else '') + ev + ('…' if e < len(text) else '')


# Primeiro achado válido do grupo: o de menor índice no texto (empate: o padrão listado antes). Achado negado ou
# barrado por guarda (exceptBefore, exceptAfter, afterChecks) não conta, e a busca segue do caractere seguinte.
def _first_hit(c, text, g):
    if not text:
        return None
    best = None
    for p in g['pats']:
        if any(_has(u, text) for u in p['unless']):
            continue
        pos = 0
        while True:
            m = p['re'].search(text, pos)
            if not m or (best and m.start() >= best['index']):
                break
            i, end = m.start(), m.end()
            if (g['noNeg'] or not _negated(c, text, i, end)) and not _guarded(c, text, i, end, p, g):
                best = {'index': i, 'end': end, 'match': m.group(0)}
                break
            pos = i + 1
    if best:
        best['evidence'] = _evidence(c, text, best['index'], best['end'])
    return best


# O prefixo "Remote <região> · " que o coletor antigo punha no local não é do anúncio: sai antes de qualquer regra.
def _views(c, job):
    title = normalize(job.get('title'))
    loc = c['synthetic'].sub('', normalize(job.get('location')))
    head = f'{title}\n{loc}'
    body = normalize(job.get('description'))
    return {'title': title, 'loc': loc, 'head': head, 'body': body, 'full': f'{head}\n{body}'}


def _remote(c, v):
    opt, cult = _first_hit(c, v['full'], c['remote_option']), _first_hit(c, v['full'], c['remote_culture'])
    conf, head_conf = _first_hit(c, v['full'], c['remote_confirm']), _first_hit(c, v['head'], c['remote_head'])
    how, h = ('opção remota', opt) if opt else ('cultura remota', cult) if cult else (('título/local' if conf['index'] < len(v['head']) else 'texto'), conf) if conf \
        else ('título/local', head_conf) if head_conf else ('', None)
    return {'opt': opt, 'cult': cult, 'confirmed': bool(h), 'how': how, 'evidence': h['evidence'] if h else ''}


def _lang_of(c, body):
    tokens = [m.group(0) for m in c['token'].finditer(body)]
    if len(tokens) < c['min_tokens']:
        return {'code': None, 'n': 0, 'en': 0}
    code, n, en = None, 0, 0
    for k in c['langs']:
        stop = c['stop'][k]
        m = sum(1 for t in tokens if t in stop)
        if k == 'en':
            en = m
        if m > n:
            code, n = k, m
    return {'code': code, 'n': n, 'en': en}


def _language(c, v):
    L = c['R']['language']
    lg = _lang_of(c, v['body'])
    when = c['lang_when'].get(lg['code']) if lg['code'] else None
    if (lg['code'] and lg['code'] not in c['lang_allowed'] and lg['n'] >= c['min_hits'] and lg['n'] > lg['en']
            and not (when and _has(when, v['head']))):
        name = (L.get('names') or {}).get(lg['code']) or lg['code']
        return {'code': lg['code'], 'evidence': f"anúncio em {name} ({lg['code']} {lg['n']} × en {lg['en']})"}
    for s in c['sentence_split'].split(f"{v['title']}\n{v['body']}"):
        s = s.strip(' ')
        if not s or _has(c['lang_optional'], s):
            continue
        if _first_hit(c, s, c['lang_require']) or _has(c['lang_local'], s):
            return {'code': lg['code'], 'evidence': f'{s[:200]}…' if len(s) > 200 else s}
    return {'code': lg['code'], 'evidence': None}


def _currency(c, job):
    text = normalize(as_text(job.get('salary')) + '\n' + as_text(job.get('description')))
    good, bad = _first_hit(c, text, c['currency_good']), _first_hit(c, text, c['currency_bad'])
    codes = [k for k, r in c['currency_codes'] if _has(r, text)]
    status = 'bad' if bad and not good else 'ok' if good or codes else 'unknown'
    return {'status': status, 'codes': codes,
            'evidence': bad['evidence'] if status == 'bad' else good['evidence'] if good else ''}


def _contractor(c, job, v):
    text = f"{normalize(job.get('tags'))}\n{v['head']}\n{v['body']}"
    for kind, g in c['contractor_types']:
        h = _first_hit(c, text, g)
        if h:
            return {'type': kind, 'evidence': h['evidence']}
    meta = normalize(job.get('employmentType'))
    if meta and _has(c['contractor_meta'], meta):
        return {'type': 'Contractor', 'evidence': f'tipo no LinkedIn: {meta}'}
    return {'type': None, 'evidence': ''}


def _country_hit(c, job, v):
    # Achado no título: só o próprio título anula. Achado no local: título ou local anulam.
    ht = _first_hit(c, v['title'], c['country_head'])
    if ht and not _has(c['country_head_unless'], v['title']):
        return ht['evidence']
    hl = None if ht else _first_hit(c, v['loc'], c['country_head'])
    if hl and not _has(c['country_head_unless'], v['head']):
        return hl['evidence']
    t = _first_hit(c, v['full'], c['country_text'])
    if t:
        return t['evidence']
    src = normalize(job.get('source'))
    if v['loc'] and any(s in src for s in c['cand_sources']) and not _has(c['cand_allowed'], v['loc']):
        return f"onde o candidato pode morar: {v['loc']}"
    return None


# País do local: o que aparece por último ("Valencia, Valencian Community, Spain" -> Spain); empate fica com o da lista.
def _country_in(c, loc):
    best, at = None, -1
    for name, r in c['countries']:
        pos, last = 0, -1
        while True:
            m = r.search(loc, pos)
            if not m:
                break
            last = m.start()
            pos = m.start() + max(1, len(m.group(0)))
        if last > at:
            at, best = last, name
    return best


def assess(job):
    """Todas as regras rodam (hits mostra tudo o que disparou); a exclusão é a primeira de "order" que disparou.
    Tirar uma chave de "order" desliga a exclusão por ela, mas o achado continua em hits."""
    c = criteria()
    R = c['R']
    job = job or {}
    v = _views(c, job)
    remote = _remote(c, v)
    # Vaga do LinkedIn: 'Remote' só no título/local não conta; precisa de frase remota no corpo.
    from_linkedin = str(job.get('id') or '').startswith('linkedin-') or 'linkedin' in str(job.get('source') or '').lower()
    if from_linkedin and R['no_remote'].get('linkedinBodyOnly') and remote['confirmed']:
        rb = _remote(c, {**v, 'head': '', 'full': v['body']})
        if not rb['confirmed']:
            remote = rb
    # Local/modo declarado pelo próprio ATS conta como frase remota (não anula ats_hybrid/ats_onsite, país, spain_local).
    if not remote['confirmed']:
        ats_loc, ats_wp = normalize(job.get('atsLocation')), str(job.get('atsWorkplace') or '').lower()
        loc_ok = bool(ats_loc) and _has(c['ats_loc_remote'], ats_loc) and not _has(c['ats_loc_not'], ats_loc)
        if (loc_ok or ats_wp == 'remote') and ats_wp not in ('hybrid', 'onsite'):
            remote = {'opt': None, 'cult': None, 'confirmed': True, 'how': 'ATS', 'evidence': ats_loc or 'workplaceType remote'}
    hits = {}
    learned_hit = None

    if job.get('closed') is True:
        hits['closed'] = 'a vaga não aceita mais candidaturas'

    sen = _first_hit(c, v['title'], c['seniority'])
    if sen:
        hits['seniority'] = sen['evidence']
    else:
        meta = normalize(job.get('seniority'))
        if meta and _first_hit(c, meta, c['seniority_meta']):
            hits['seniority'] = f'nível no LinkedIn: {meta}'

    for l in c['learned']:
        f = l['field']
        text = v['full'] if f == 'text' else v['title'] if f == 'title' else v['loc'] if f == 'location' else normalize(job.get('company'))
        h = _first_hit(c, text, l['g'])
        if h:
            hits['learned'] = h['evidence']
            learned_hit = l
            break

    ats_w = str(job.get('atsWorkplace') or '').lower()
    if ats_w == 'onsite':
        hits['ats_onsite'] = 'ATS (workplaceType) declara presencial'
    if ats_w == 'hybrid':
        hits['ats_hybrid'] = 'ATS (workplaceType) declara híbrido'

    country = _country_hit(c, job, v)
    if country:
        hits['country'] = country

    explicit, label = _first_hit(c, v['full'], c['onsite_explicit']), _first_hit(c, v['full'], c['onsite_labels'])
    if explicit and not remote['opt']:
        hits['onsite'] = explicit['evidence']
    elif label and not remote['opt'] and not remote['cult']:
        hits['onsite'] = label['evidence']

    reloc = _first_hit(c, v['full'], c['relocation'])
    if reloc:
        hits['relocation'] = reloc['evidence']
    auth = _first_hit(c, v['full'], c['authorization'])
    if auth:
        hits['authorization'] = auth['evidence']

    if not v['body']:
        hits['needs_description'] = 'sem texto da vaga'
    elif len(v['body']) < c['min_desc'] and job.get('descriptionFinal') is not True:
        hits['needs_description'] = f"texto curto ({len(v['body'])} caracteres)"

    spain = _has(c['spain_places'], v['loc']) or any(_has(r, v['head']) for r in c['spain_head'])
    if spain and not _first_hit(c, v['full'], c['spain_foreign']):
        hits['spain_local'] = f"vaga na Espanha ({v['loc'] or v['title']}) sem EOR, contractor, freelance ou B2B no anúncio"

    generic = remote['confirmed'] and _has(c['remote_generic_ev'], remote['evidence'] or '') and _has(c['remote_generic_ctx'], v['full'])
    if generic:
        remote = {**remote, 'confirmed': False}
    friendly = not generic and not remote['confirmed'] and _has(c['remote_friendly'], v['full'])
    if not remote['confirmed'] and not friendly and not generic:
        hits['no_remote'] = 'nenhuma frase do anúncio diz que é remoto'

    lang = _language(c, v)
    if lang['evidence']:
        hits['language'] = lang['evidence']

    currency = _currency(c, job)
    if currency['status'] == 'bad':
        hits['currency'] = currency['evidence']

    contractor = _contractor(c, job, v)

    key = next((k for k in c['order'] if k in hits), None)
    exclude = None
    if key:
        exclude = {'key': key, 'label': R[key]['label'], 'ui': R[key]['ui'], 'evidence': hits[key]}
        if key == 'learned':
            exclude.update(label=f"{R['learned']['label']}: {learned_hit['label']}", ui=learned_hit['ui'], rule=learned_hit['id'])
    labels = []
    if contractor['type']:
        labels.append(contractor['type'])
    if currency['status'] == 'unknown':
        labels.append('moeda?')
    if friendly:
        labels.append('confirmar: remote friendly')
    if generic:
        labels.append('confirmar: remoto genérico')
    if contractor['type'] and _has(c['spanish_base'], v['body']) and not _has(c['foreign_entity'], v['full']):
        labels.append('confirmar: freelance empresa espanhola')
    if _has(c['seniority_confirm'], v['title']):
        labels.append('confirmar: principal')
    return {
        'exclude': exclude,
        'remote': {'confirmed': remote['confirmed'], 'how': remote['how'], 'evidence': remote['evidence']},
        'contractor': contractor, 'currency': currency, 'country': _country_in(c, v['loc']), 'lang': lang['code'],
        'labels': labels, 'hits': hits,
    }


# ---- API ----
def hard_exclude(job):
    return assess(job)['exclude']


def hard_exclude_reason(job):
    x = hard_exclude(job)
    return x['label'] if x else None


def needs_description(job):
    """Só falta o texto da vaga pra decidir (nenhuma regra anterior já exclui)."""
    x = hard_exclude(job)
    return bool(x) and x['key'] == 'needs_description'


def labels_for(job):
    return assess(job)['labels']


def detect_lang(text):
    return _lang_of(criteria(), normalize(text))['code']


def country_of(location):
    c = criteria()
    return _country_in(c, c['synthetic'].sub('', normalize(location)))


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd not in ('assess', 'normalize'):
        print('uso: python3 criteria.py assess|normalize < lista.json', file=sys.stderr)
        return 2
    data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    out = [assess(j) for j in data] if cmd == 'assess' else [normalize(s) for s in data]
    sys.stdout.write(json.dumps(out, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv))
