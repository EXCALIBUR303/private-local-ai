/* ══════════════════════════════════════════════════════════════════════
   Private Local AI — interface logic.

   No frameworks, no CDNs, no remote anything. Everything runs from disk.

   The two pieces worth reading are Core (the ambient visual that reflects
   real application state rather than decorating) and the citation linking,
   which ties inline [n] markers to their source cards in both directions.
   ══════════════════════════════════════════════════════════════════════ */
'use strict';

/* ───────────────────────────── helpers ───────────────────────────── */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function toast(message, kind = '') {
  const t = el('div', 'toast ' + kind, message);
  $('toasts').appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transform = 'translateX(12px)';
    setTimeout(() => t.remove(), 260);
  }, 5200);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

const escapeHtml = (s) => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const fmtBytes = (n) => {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), 3);
  return `${(n / 1024 ** i).toFixed(i > 1 ? 1 : 0)} ${u[i]}`;
};
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString();
const fmtClock = (s) => {
  if (s == null) return '—';
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}:${String(r).padStart(2, '0')}`;
};
const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* Safe Markdown: escape first, then re-introduce only this whitelist.
   Citation markers become interactive .cref elements. */
function renderMarkdown(src) {
  const fences = [];
  let s = escapeHtml(src);

  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_m, lang, code) => {
    fences.push(`<pre><code data-lang="${escapeHtml(lang)}">${code.replace(/\n$/, '')}</code></pre>`);
    return `\uE000${fences.length - 1}\uE001`;
  });

  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/^###\s+(.+)$/gm, '<h4>$1</h4>');
  s = s.replace(/^##\s+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/^#\s+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/\[(\d{1,2})\]/g,
    (_m, n) => `<span class="cref" data-n="${n}" role="button" tabindex="0">${n}</span>`);

  const lines = s.split('\n');
  const out = [];
  let list = null;
  for (const line of lines) {
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*(\d+)\.\s+(.*)$/);
    if (ul) {
      if (list !== 'ul') { if (list) out.push(`</${list}>`); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${ul[1]}</li>`);
    } else if (ol) {
      if (list !== 'ol') { if (list) out.push(`</${list}>`); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${ol[2]}</li>`);
    } else {
      if (list) { out.push(`</${list}>`); list = null; }
      out.push(line.trim() === '' ? '' : `<p>${line}</p>`);
    }
  }
  if (list) out.push(`</${list}>`);
  s = out.join('\n').replace(/<p>(<h[34]>)/g, '$1').replace(/(<\/h[34]>)<\/p>/g, '$1');
  s = s.replace(/\uE000(\d+)\uE001/g, (_m, i) => fences[Number(i)]);
  // A <pre> may not live inside a <p>; browsers would silently split the
  // paragraph and the block would inherit the wrong styles.
  s = s.replace(/<p>\s*(<pre>)/g, '$1').replace(/(<\/pre>)\s*<\/p>/g, '$1');
  return s;
}

/* ══════════════════════ Core: ambient state visual ══════════════════════
   Three orbiting arc rings around a soft centre. Speed and brightness are
   driven by what the application is actually doing — idle, generating, or
   listening — so it reports state rather than merely decorating.        */
class Core {
  constructor(canvas, { rings = 3, base = 0.34 } = {}) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.rings = rings;
    this.base = base;
    this.t = 0;
    this.mode = 'idle';        // idle | busy | listen
    this.energy = 0;           // 0..1, eased toward the mode's target
    this.amp = 0;              // live mic amplitude when listening
    this.resize();
    this._onResize = () => this.resize();
    window.addEventListener('resize', this._onResize);
    this.loop = this.loop.bind(this);
    requestAnimationFrame(this.loop);
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = this.c.getBoundingClientRect();
    const w = Math.max(r.width, 1), h = Math.max(r.height, 1);
    this.c.width = Math.round(w * dpr);
    this.c.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w; this.h = h;
  }

  set(mode) { this.mode = mode; }
  setAmp(v) { this.amp = v; }

  loop() {
    const target = this.mode === 'busy' ? 1 : this.mode === 'listen' ? 0.7 : 0;
    this.energy += (target - this.energy) * 0.06;
    this.t += 0.004 + this.energy * 0.026;

    const { ctx, w, h } = this;
    const cx = w / 2, cy = h / 2;
    const R = Math.min(w, h) / 2 - 3;
    ctx.clearRect(0, 0, w, h);
    if (R <= 4) { requestAnimationFrame(this.loop); return; }

    const amber = cssVar('--amber') || '#f0a13c';
    const mint = cssVar('--mint') || '#4fd6a3';
    const live = this.mode === 'listen';
    const hue = live ? mint : amber;
    const detail = R > 40;               // the rail copy is tiny; skip fine work

    // ── bezel: a ticked dial, so this reads as an instrument, not a blob
    if (detail) {
      const ticks = 60;
      for (let i = 0; i < ticks; i++) {
        const a = (i / ticks) * Math.PI * 2;
        const major = i % 5 === 0;
        const len = major ? R * 0.075 : R * 0.038;
        const lit = ((i / ticks + this.t * 0.16) % 1) < 0.14;   // travelling sweep
        ctx.strokeStyle = hexA(hue, lit ? 0.30 + this.energy * 0.4
                                        : (major ? 0.17 : 0.09));
        ctx.lineWidth = major ? 1.4 : 1;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
        ctx.lineTo(cx + Math.cos(a) * (R - len), cy + Math.sin(a) * (R - len));
        ctx.stroke();
      }
    }
    ctx.strokeStyle = hexA(hue, 0.13 + this.energy * 0.14);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, R * (detail ? 0.885 : 1), 0, Math.PI * 2);
    ctx.stroke();

    // ── orbiting arcs
    const inner = R * (detail ? 0.80 : 0.92);
    for (let i = 0; i < this.rings; i++) {
      const f = this.rings === 1 ? 1 : i / (this.rings - 1);
      const rad = inner * (0.46 + f * 0.5);
      const dir = i % 2 ? -1 : 1;
      const spin = this.t * (0.8 + i * 0.5) * dir;
      const seg = 0.62 + i * 0.42;
      const alpha = Math.min(0.95, (this.base + 0.22 + this.energy * 0.45) * (1 - i * 0.1));

      ctx.strokeStyle = hexA(hue, alpha);
      ctx.lineWidth = Math.max(1.4, R * 0.028);
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.arc(cx, cy, rad, spin, spin + seg);
      ctx.stroke();

      ctx.strokeStyle = hexA(hue, alpha * 0.45);
      ctx.beginPath();
      ctx.arc(cx, cy, rad, spin + Math.PI, spin + Math.PI + seg * 0.5);
      ctx.stroke();

      const nx = cx + Math.cos(spin + seg) * rad;
      const ny = cy + Math.sin(spin + seg) * rad;
      ctx.fillStyle = hexA(hue, Math.min(1, alpha + 0.35));
      ctx.beginPath();
      ctx.arc(nx, ny, Math.max(1.4, R * 0.03), 0, Math.PI * 2);
      ctx.fill();
    }

    // ── centre: contained glow plus a solid core, so it stays crisp
    const pulse = 1 + Math.sin(this.t * 3.1) * (0.06 + this.energy * 0.14)
                    + (live ? this.amp * 0.55 : 0);
    const gR = R * 0.34 * pulse;
    const gr = ctx.createRadialGradient(cx, cy, 0, cx, cy, gR);
    gr.addColorStop(0, hexA(hue, 0.5 + this.energy * 0.35));
    gr.addColorStop(0.55, hexA(hue, 0.14 + this.energy * 0.16));
    gr.addColorStop(1, hexA(hue, 0));
    ctx.fillStyle = gr;
    ctx.beginPath();
    ctx.arc(cx, cy, gR, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = hexA(hue, 0.85 + this.energy * 0.15);
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(1.6, R * 0.055 * pulse), 0, Math.PI * 2);
    ctx.fill();

    requestAnimationFrame(this.loop);
  }
}

/* colour helper: accepts #rgb/#rrggbb or any rgb()/oklch() string */
function hexA(color, a) {
  const c = String(color).trim();
  if (c.startsWith('#')) {
    let h = c.slice(1);
    if (h.length === 3) h = h.split('').map((x) => x + x).join('');
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  const m = c.match(/-?\d+\.?\d*/g);
  if (m && m.length >= 3) return `rgba(${m[0]}, ${m[1]}, ${m[2]}, ${a})`;
  return `rgba(240, 161, 60, ${a})`;
}

/* ═══════════════════ live microphone waveform ═══════════════════ */
class Waveform {
  constructor(canvas) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d');
    this.data = null;
    this.running = false;
  }
  start(stream) {
    this.ctx2 = new (window.AudioContext || window.webkitAudioContext)();
    const src = this.ctx2.createMediaStreamSource(stream);
    this.analyser = this.ctx2.createAnalyser();
    this.analyser.fftSize = 256;
    src.connect(this.analyser);
    this.data = new Uint8Array(this.analyser.frequencyBinCount);
    this.running = true;
    this.draw = this.draw.bind(this);
    requestAnimationFrame(this.draw);
  }
  async stop() {
    this.running = false;
    try { await this.ctx2?.close(); } catch { /* already closed */ }
    this.ctx2 = null;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.ctx.clearRect(0, 0, this.c.width / dpr, this.c.height / dpr);
  }
  draw() {
    if (!this.running || !this.analyser) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const r = this.c.getBoundingClientRect();
    if (this.c.width !== Math.round(r.width * dpr)) {
      this.c.width = Math.round(r.width * dpr);
      this.c.height = Math.round(r.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    const w = r.width, h = r.height, cx = w / 2, cy = h / 2;
    this.analyser.getByteFrequencyData(this.data);

    let sum = 0;
    for (const v of this.data) sum += v;
    const level = Math.min(1, sum / this.data.length / 90);
    state.core?.setAmp(level);

    const ctx = this.ctx;
    ctx.clearRect(0, 0, w, h);
    const bars = 28;
    const baseR = Math.min(w, h) * 0.30;
    const amber = cssVar('--amber') || '#f0a13c';
    for (let i = 0; i < bars; i++) {
      const idx = Math.floor((i / bars) * this.data.length * 0.7);
      const v = this.data[idx] / 255;
      const a = (i / bars) * Math.PI * 2 - Math.PI / 2;
      const r0 = baseR;
      const r1 = baseR + 3 + v * Math.min(w, h) * 0.17;
      ctx.strokeStyle = hexA(amber, 0.35 + v * 0.55);
      ctx.lineWidth = 2;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      ctx.stroke();
    }
    requestAnimationFrame(this.draw);
  }
}

/* ───────────────────────────── state ───────────────────────────── */
const state = {
  conversationId: null,
  streaming: false,
  useDocs: false,
  status: null,
  docCount: 0,
  core: null,
  brandCore: null,
  wave: null,
  recorder: null,
  chunks: [],
  stream: null,
  recording: false,
  deletePhrase: 'DELETE ALL MY LOCAL DATA',
  lastSpeed: null,
  warnedOffline: false,
  persona: 'direct',
  personas: {},
  speechPoll: null,
  autonomy: 'off',
  agentRun: null,
  routeInfo: null,
  voice: null,          // null = built-in engine, otherwise a cloned voice name
  voices: [],
  names: {},
  byModel: {},
  cloneReady: false,
  muted: false,
  mode: 'Core',
  modes: {},
  recWave: null,
  recTimer: null,
  recStart: 0,
  recBlob: null,
  useWatch: false,
  watchTimer: null,
  watchCatalog: null,
  egress: null,
  buildRun: false,
  buildAbort: null,
  codeSurvey: null,
};

/* ───────────────────────────── SSE ───────────────────────────── */
async function readSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || '';
    for (const part of parts) {
      const line = part.split('\n').find((l) => l.startsWith('data: '));
      if (!line) continue;
      try { onEvent(JSON.parse(line.slice(6))); } catch { /* partial frame */ }
    }
  }
}

/* ─────────────────────── status + live vitals ─────────────────────── */
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    state.status = s;
    const ok = s.runtime.reachable;

    $('beacon').classList.toggle('offline', !ok);
    $('claimTitle').textContent = ok ? 'Local only' : 'Runtime offline';
    $('claimSub').textContent = ok ? 'no cloud API' : 'start ollama';
    $('brandSub').textContent = ok ? 'on-device' : 'offline';

    $('gCtx').textContent = `${s.models.num_ctx} tok`;
    state.docCount = s.documents.sources;
    $('navDocCount').textContent = s.documents.sources || '';
    $('toggleCount').textContent = s.documents.chunks
      ? `${s.documents.chunks}` : '';

    const sel = $('modelSelect');
    const chat = s.runtime.models.filter((m) => !m.includes('embed'));
    if (sel.options.length !== chat.length) {
      const current = sel.value || s.models.chat;
      sel.innerHTML = '';
      const auto = el('option', null, 'auto — route by task');
      auto.value = 'auto';
      sel.appendChild(auto);
      for (const m of chat) {
        const nice = friendlyName(m);
        const o = el('option', null, nice === m ? m : `${nice} \u2014 ${m}`);
        o.value = m;
        sel.appendChild(o);
      }
      sel.value = current === 'auto' || chat.includes(current) ? current : 'auto';
    }

    const mic = $('micBtn');
    mic.disabled = !s.speech.stt.available;
    if (!state.recording) {
      $('micHint').textContent = s.speech.stt.available
        ? 'Hold the microphone to record. Audio is transcribed on this Mac.'
        : 'Voice input unavailable: ' + (s.speech.stt.reasons[0] || 'not configured');
    }

    if (!ok && !state.warnedOffline) {
      state.warnedOffline = true;
      toast('Local model runtime is not reachable. Run: ollama serve', 'err');
    }
    if (ok) state.warnedOffline = false;
  } catch (e) {
    toast('Status check failed: ' + e.message, 'err');
  }
}

async function refreshVitals() {
  try {
    const v = await api('/api/vitals');
    // Several models can be resident at once — the embedding model is loaded
    // during retrieval. Report the one actually driving the conversation,
    // falling back to whichever is holding the most memory.
    const active = $('modelSelect').value;
    const list = v.loaded || [];
    const loaded =
      list.find((m) => m.name === active || m.name === `${active}:latest`) ||
      list.slice().sort((a, b) => (b.bytes || 0) - (a.bytes || 0))[0];
    if (loaded) {
      $('gModel').textContent = friendlyName(loaded.name);
      $('gModel').classList.add('hot');
      $('gResident').textContent = fmtBytes(loaded.bytes);
      $('gCtx').textContent = loaded.context_length
        ? `${loaded.context_length} tok` : $('gCtx').textContent;
      $('claimSub').textContent = loaded.unloads_in != null
        ? `unloads in ${fmtClock(loaded.unloads_in)}`
        : 'no cloud API';
    } else {
      $('gModel').textContent = state.status?.models.chat || '—';
      $('gModel').classList.remove('hot');
      $('gResident').textContent = 'not loaded';
      if (state.status?.runtime.reachable) $('claimSub').textContent = 'no cloud API';
    }
    $('gSpeed').textContent = state.lastSpeed ? `${state.lastSpeed} tok/s` : '—';

    const mem = v.memory || {};
    if (mem.free_percent != null && mem.total_bytes) {
      const used = 100 - mem.free_percent;
      $('memFill').style.width = `${Math.max(2, Math.min(100, used))}%`;
      $('memLabel').textContent =
        `memory ${used.toFixed(0)}% of ${fmtBytes(mem.total_bytes)}`;
    }
  } catch { /* vitals are ambient; never disrupt the page */ }
}

/* ══════════════════════ persona (voice + manner) ══════════════════════ */
async function loadModes() {
  try {
    const d = await api('/api/modes');
    state.modes = d.modes || {};
    const saved = localStorage.getItem('pai.mode');
    state.mode = state.modes[saved] ? saved : (d.default || 'Core');
  } catch { state.modes = {}; state.mode = 'Core'; }

  const sel = $('modeSelect');
  sel.innerHTML = '';
  for (const [name, m] of Object.entries(state.modes)) {
    const o = el('option', null, m.lanes > 1 ? `${name} ×${m.lanes}` : name);
    o.value = name;
    o.disabled = !m.available;
    sel.appendChild(o);
  }
  sel.value = state.mode;
  applyMode();
}

function applyMode() {
  const m = state.modes[state.mode];
  if (!m) return;
  $('modeSelect').title = `${m.blurb}\n${m.detail}`;
  $('modelSelect').value = m.model;
  const lanes = $('lanesInput');
  if (lanes) lanes.hidden = (m.lanes || 1) < 2;
  try { localStorage.setItem('pai.mode', state.mode); } catch { /* private window */ }
}

async function loadSkills() {
  try {
    const d = await api('/api/skills');
    const entries = Object.entries(d.skills);
    $('skillCount').textContent = String(entries.length);
    const list = $('skillList');
    list.innerHTML = '';
    if (!entries.length) {
      list.appendChild(el('div', 'dim', `No skills yet. Files live in ${d.dir}`));
      return;
    }
    for (const [name, sk] of entries) {
      const row = el('div', 'skill-row');
      const tag = el('span', 'tag', sk.kind === 'command' ? sk.risk : 'guide');
      tag.classList.add(sk.kind === 'command'
        ? (sk.risk === 'read' ? 'tag-ok' : sk.risk === 'destructive' ? 'tag-no' : 'tag-warn')
        : 'tag-ok');
      row.appendChild(tag);
      const body = el('div', 'skill-body');
      const head = el('div');
      head.appendChild(el('code', null, name));
      body.appendChild(head);
      body.appendChild(el('div', 'skill-meta', sk.description || sk.file));
      if (sk.params?.length) {
        body.appendChild(el('div', 'skill-meta', `takes: ${sk.params.join(', ')}`));
      }
      row.appendChild(body);
      const del = el('button', 'mini', 'delete');
      del.addEventListener('click', async () => {
        await api(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
        loadSkills();
      });
      row.appendChild(del);
      list.appendChild(row);
    }
  } catch (e) { toast(e.message, 'err'); }
}

function newSkill() {
  let nameField, textField;
  showModal('New skill', (body) => {
    body.appendChild(el('p', 'dim',
      'A skill is one Markdown file. Add a fenced bash block and run: shell to make it a runnable tool.'));
    nameField = el('input');
    nameField.type = 'text';
    nameField.placeholder = 'skill-name';
    body.appendChild(nameField);
    textField = document.createElement('textarea');
    textField.value = [
      '---', 'name: my-skill',
      'description: What this does',
      'when: when to reach for it',
      'run: shell', '---',
      'Notes for the model go here.', '',
      '```bash', 'echo {{argument}}', '```',
    ].join('\n');
    body.appendChild(textField);
  }, [
    { label: 'Cancel', cls: 'btn-quiet', onClick: (c) => c() },
    { label: 'Save skill', cls: 'btn-accent', onClick: async (c) => {
        try {
          await api('/api/skills', {
            method: 'POST',
            body: JSON.stringify({ name: nameField.value.trim(), text: textField.value }),
          });
          c(); toast('Skill saved.', 'ok'); loadSkills();
        } catch (e) { toast(e.message, 'err'); }
      } },
  ]);
}

async function loadNames() {
  try {
    const d = await api('/api/names');
    state.names = d.names || {};
    state.byModel = {};
    for (const [friendly, meta] of Object.entries(state.names)) {
      state.byModel[meta.model] = friendly;
    }
  } catch { state.names = {}; state.byModel = {}; }
}

/* 'qwen3:8b' -> 'Core'. Unknown ids keep their own name. */
function friendlyName(id) {
  if (!id) return '\u2014';
  return state.byModel[id] || state.byModel[id.replace(':latest', '')] || id;
}

async function loadPersonas() {
  try {
    const d = await api('/api/personas');
    state.personas = d.personas;
    const saved = localStorage.getItem('pai.persona');
    state.persona = state.personas[saved] ? saved : d.default;
  } catch {
    state.personas = { direct: { label: 'Direct', speak_by_default: false } };
    state.persona = 'direct';
  }
  const box = $('personaSwitch');
  box.innerHTML = '';
  for (const [key, p] of Object.entries(state.personas)) {
    const b = el('button', 'persona-opt', p.label);
    b.setAttribute('role', 'radio');
    b.title = p.blurb || p.label;
    b.addEventListener('click', () => setPersona(key));
    box.appendChild(b);
  }
  setPersona(state.persona, { announce: false });
}

function setPersona(key, { announce = true } = {}) {
  if (!state.personas[key]) return;
  state.persona = key;
  try { localStorage.setItem('pai.persona', key); } catch { /* private window */ }
  [...$('personaSwitch').children].forEach((b, i) =>
    b.setAttribute('aria-checked',
      String(Object.keys(state.personas)[i] === key)));
  const p = state.personas[key];
  if (announce) toast(`${p.label}${p.speak_by_default ? ' — replies are spoken aloud' : ''}`);
}

/* ═══════════════════════════ chat ═══════════════════════════ */
function addMessage(role, content, citations) {
  $('chatEmpty')?.remove();
  const wrap = el('div', `msg msg-${role}`);

  const head = el('div', 'msg-head');
  const who = el('div', 'who');
  who.appendChild(el('span', 'tick'));
  who.appendChild(el('span', null, role === 'user' ? 'You' : 'Local model'));
  head.appendChild(who);

  const bubble = el('div', 'bubble');
  bubble.dataset.raw = content || '';

  if (role === 'assistant') {
    const tools = el('div', 'msg-tools');
    const speak = el('button', 'mini', 'speak');
    speak.addEventListener('click', () => speakText(bubble.dataset.raw || '', speak));
    const copy = el('button', 'mini', 'copy');
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(bubble.dataset.raw || '');
        copy.textContent = 'copied';
        setTimeout(() => (copy.textContent = 'copy'), 1400);
      } catch { toast('Clipboard unavailable', 'err'); }
    });
    tools.append(speak, copy);
    head.appendChild(tools);
  }
  wrap.appendChild(head);

  bubble.innerHTML = content ? renderMarkdown(content) : '';
  wrap.appendChild(bubble);

  const box = el('div', 'sources-box');
  wrap.appendChild(box);
  if (citations && citations.length) renderSources(wrap, box, citations);

  $('messages').appendChild(wrap);
  if (content) linkCitations(wrap);
  scrollDown();
  return { wrap, bubble, box };
}

/* Two-way linking between inline [n] markers and their source cards. */
function linkCitations(scope) {
  const cards = new Map();
  scope.querySelectorAll('.src-card').forEach((c) => cards.set(c.dataset.n, c));

  const lightUp = (n, on) => {
    const card = cards.get(String(n));
    if (card) card.classList.toggle('lit', on);
    scope.querySelectorAll(`.cref[data-n="${n}"]`)
      .forEach((r) => r.classList.toggle('lit', on));
  };

  scope.querySelectorAll('.cref').forEach((ref) => {
    const n = ref.dataset.n;
    ref.addEventListener('mouseenter', () => lightUp(n, true));
    ref.addEventListener('mouseleave', () => lightUp(n, false));
    ref.addEventListener('focus', () => lightUp(n, true));
    ref.addEventListener('blur', () => lightUp(n, false));
    const jump = () => {
      const card = cards.get(String(n));
      if (!card) return;
      card.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'nearest' });
      card.classList.add('lit');
      setTimeout(() => card.classList.remove('lit'), 1300);
    };
    ref.addEventListener('click', jump);
    ref.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
    });
  });

  cards.forEach((card, n) => {
    card.addEventListener('mouseenter', () => lightUp(n, true));
    card.addEventListener('mouseleave', () => lightUp(n, false));
  });
}

function renderSources(scope, box, citations) {
  box.innerHTML = '';
  if (!citations || !citations.length) return;
  box.appendChild(el('div', 'sources-head', `${citations.length} source${citations.length === 1 ? '' : 's'}`));

  for (const c of citations) {
    const card = el('div', 'src-card' + (c.suspicious ? ' flagged' : ''));
    card.dataset.n = String(c.n);
    card.appendChild(el('div', 'src-n', String(c.n)));

    const body = el('div', 'src-body');
    const title = el('div', 'src-title');
    title.appendChild(el('span', null, c.filename));
    if (c.locator) title.appendChild(el('span', 'src-loc', '  ' + c.locator));
    body.appendChild(title);
    body.appendChild(el('div', 'src-snip', c.snippet));

    if (c.suspicious) {
      body.appendChild(el('div', 'src-flag',
        'This passage contains instruction-like text. It was given to the model as data only.'));
    }
    const open = el('button', 'mini src-open', 'open file');
    open.addEventListener('click', () => proposeAction('open_file', { path: c.path }));
    body.appendChild(open);

    card.appendChild(body);
    box.appendChild(card);
  }
  linkCitations(scope);
}

/*
 * Scroll to the newest message — but only if the reader was already there.
 * Yanking someone back to the bottom while they are reading earlier output is
 * the single most irritating thing a streaming interface can do.
 */
const NEAR_BOTTOM = 120;
const scrollDown = (force = false) => {
  const m = $('messages');
  const distance = m.scrollHeight - m.scrollTop - m.clientHeight;
  if (force || distance < NEAR_BOTTOM) m.scrollTop = m.scrollHeight;
};

/*
 * Repaint a streaming bubble at most once per animation frame.
 *
 * The naive version re-parsed and re-rendered the whole accumulated answer on
 * every single token. At twenty-odd tokens a second that is twenty full
 * markdown parses and innerHTML swaps a second over text that keeps growing —
 * quadratic work, and visible stutter by the third paragraph. Coalescing to
 * one paint per frame makes the cost constant per frame no matter how fast the
 * model emits, and the text still appears to flow.
 */
function makePainter(bubble) {
  let pending = null;
  let frame = 0;
  const paint = () => {
    frame = 0;
    if (pending === null) return;
    bubble.dataset.raw = pending;
    bubble.innerHTML = renderMarkdown(pending);
    pending = null;
    scrollDown();
  };
  return {
    push(text) {
      pending = text;
      if (!frame) frame = requestAnimationFrame(paint);
    },
    flush(text) {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      pending = text;
      paint();
    },
  };
}

function setBusy(on, label) {
  state.streaming = on;
  $('sendBtn').disabled = on;
  const g = $('genStatus');
  g.classList.toggle('busy', on);
  g.textContent = on ? (label || 'generating locally') : '';
  state.core?.set(on ? 'busy' : 'idle');
  state.brandCore?.set(on ? 'busy' : 'idle');
}

async function sendMessage(preset) {
  if (state.streaming) return;
  const input = $('input');
  const text = (preset != null ? preset : input.value).trim();
  if (!text) return;

  input.value = '';
  input.style.height = 'auto';
  addMessage('user', text);

  const { wrap, bubble, box } = addMessage('assistant', '');
  wrap.classList.add('streaming');
  const painter = makePainter(bubble);
  setBusy(true);

  let acc = '';
  let tokens = 0;
  // Measure generation speed from the FIRST token, not from the request:
  // retrieval and a cold model load are not part of throughput.
  let tFirst = 0, tLast = 0;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        conversation_id: state.conversationId,
        model: $('modelSelect').value || 'auto',
        use_documents: state.useDocs,
        use_watchers: state.useWatch,
        persona: state.persona,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }

    let failure = null;
    await readSSE(res, (ev) => {
      if (ev.type === 'meta') {
        state.conversationId = ev.conversation_id;
        const chip = $('routeChip');
        if (ev.route) {
          chip.hidden = false;
          chip.classList.add('on');
          chip.textContent = `${ev.route.label} · ${ev.model}`;
          chip.title = `${ev.route.reason}\n${ev.route.why}`;
        } else {
          chip.hidden = false;
          chip.classList.remove('on');
          chip.textContent = ev.friendly || friendlyName(ev.model);
          chip.title = 'Model chosen manually';
        }
      } else if (ev.type === 'status') {
        $('genStatus').textContent = ev.text.replace(/…$/, '');
      } else if (ev.type === 'citations') {
        renderSources(wrap, box, ev.citations);
        $('genStatus').textContent = ev.citations.length
          ? `${ev.citations.length} passages retrieved`
          : 'no document matched';
      } else if (ev.type === 'token') {
        acc += ev.text;
        tokens += 1;
        tLast = performance.now();
        if (!tFirst) tFirst = tLast;
        painter.push(acc);
      } else if (ev.type === 'done') {
        acc = ev.answer || acc;
        painter.flush(acc);
        renderSources(wrap, box, ev.citations);
      } else if (ev.type === 'error') {
        failure = ev.detail;
      }
    });
    if (failure) throw new Error(failure);

    const secs = (tLast - tFirst) / 1000;
    if (tokens > 4 && secs > 0.2) state.lastSpeed = ((tokens - 1) / secs).toFixed(1);

    // A speaking persona answers out loud, the way you would expect of an
    // assistant you talk to rather than type at.
    if (state.personas[state.persona]?.speak_by_default && acc.trim()) {
      speakText(acc);
    }
  } catch (e) {
    painter.flush(acc);
    bubble.innerHTML = '';
    const err = el('p', null, e.message);
    err.style.color = 'var(--danger)';
    bubble.appendChild(err);
    toast(e.message, 'err');
  } finally {
    wrap.classList.remove('streaming');
    setBusy(false);
    linkCitations(wrap);
    loadThreads();
    refreshStatus();
    refreshVitals();
    scrollDown();
  }
}

/* ───────────────────────── threads ───────────────────────── */
async function loadThreads() {
  try {
    const { conversations } = await api('/api/conversations');
    const list = $('threadList');
    list.innerHTML = '';
    if (!conversations.length) {
      const empty = el('div', 'dim', 'No threads yet.');
      empty.style.padding = '6px 9px';
      empty.style.fontSize = '.78rem';
      list.appendChild(empty);
      return;
    }
    for (const c of conversations) {
      const item = el('div', 'thread' + (c.id === state.conversationId ? ' active' : ''));
      item.appendChild(el('span', 'thread-title', c.title));
      const x = el('button', 'thread-x', '×');
      x.title = 'Delete thread';
      x.addEventListener('click', async (e) => {
        e.stopPropagation();
        await api(`/api/conversations/${c.id}`, { method: 'DELETE' });
        if (state.conversationId === c.id) newThread();
        loadThreads();
      });
      item.appendChild(x);
      item.addEventListener('click', () => openThread(c.id));
      list.appendChild(item);
    }
  } catch { /* the rail is non-critical */ }
}

async function openThread(id) {
  const { messages } = await api(`/api/conversations/${id}`);
  state.conversationId = id;
  $('messages').innerHTML = '';
  for (const m of messages) {
    if (m.role === 'system') continue;
    addMessage(m.role, m.content, m.citations);
  }
  loadThreads();
  closeRail();
  scrollDown();
}

function newThread() {
  state.conversationId = null;
  const box = $('messages');
  box.innerHTML = '';

  const hero = el('div', 'hero');
  hero.id = 'chatEmpty';
  const canvas = document.createElement('canvas');
  canvas.className = 'hero-core';
  canvas.id = 'heroCore';
  canvas.setAttribute('aria-hidden', 'true');
  hero.appendChild(canvas);
  hero.appendChild(el('h2', null, 'Everything here stays on this Mac.'));
  hero.appendChild(el('p', null,
    'Prompts, replies and documents are processed by a model running on your own hardware. Nothing is sent anywhere.'));
  const chips = el('div', 'chips');
  chips.id = 'starterChips';
  hero.appendChild(chips);
  box.appendChild(hero);

  state.core = new Core(canvas, { rings: 3, base: 0.3 });
  renderStarters();
  loadThreads();
}

function renderStarters() {
  const row = $('starterChips');
  if (!row) return;
  const chips = [
    ['What can this app do?', false],
    ['Summarise my indexed documents', true],
    ['What are the main risks in my files?', true],
  ];
  row.innerHTML = '';
  for (const [label, wantsDocs] of chips) {
    const b = el('button', 'chip', label);
    b.addEventListener('click', () => {
      if (wantsDocs) setUseDocs(true);
      sendMessage(label);
    });
    row.appendChild(b);
  }
}

function setUseWatch(on) {
  state.useWatch = on;
  $('useWatchBtn').setAttribute('aria-checked', String(on));
  if (on) toast('Replies will include what the watchers found');
}

function setUseDocs(on) {
  state.useDocs = on;
  $('useDocsBtn').setAttribute('aria-checked', String(on));
}

/* ───────────────────────── documents ───────────────────────── */
async function previewFolder() {
  const path = $('folderPath').value.trim();
  if (!path) return;
  const out = $('previewOut');
  out.textContent = 'Scanning…';
  try {
    const r = await api('/api/documents/preview', {
      method: 'POST', body: JSON.stringify({ path }),
    });
    out.innerHTML = '';
    out.appendChild(el('div', 'dim',
      `${r.count} supported file${r.count === 1 ? '' : 's'} in ${r.folder}`));
    const list = el('div', 'rows');
    list.style.marginTop = '8px';
    for (const f of r.files.slice(0, 40)) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'tag', f.ext.replace('.', '')));
      row.appendChild(el('span', 'grow', f.name));
      row.appendChild(el('span', 'dim', fmtBytes(f.bytes)));
      list.appendChild(row);
    }
    if (r.count > 40) list.appendChild(el('div', 'dim', `and ${r.count - 40} more`));
    out.appendChild(list);
    $('indexBtn').disabled = r.count === 0;
  } catch (e) {
    out.innerHTML = '';
    const bad = el('div', 'row');
    bad.appendChild(el('span', 'tag tag-no', 'refused'));
    bad.appendChild(el('span', 'grow', e.message));
    out.appendChild(bad);
    $('indexBtn').disabled = true;
  }
}

async function indexFolder() {
  const path = $('folderPath').value.trim();
  if (!path) return;
  const prog = $('indexProgress'), fill = $('barFill'), label = $('progressText');
  prog.hidden = false;
  fill.style.width = '0%';
  label.textContent = 'Starting…';
  $('indexBtn').disabled = true;
  state.brandCore?.set('busy');

  try {
    const res = await fetch('/api/documents/index_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `Failed (${res.status})`);
    }
    let failure = null;
    await readSSE(res, (ev) => {
      if (ev.event === 'file') {
        fill.style.width = Math.round((ev.index / ev.total) * 100) + '%';
        const r = ev.result;
        label.textContent = `${ev.index}/${ev.total}  ${r.file}` +
          (r.ok ? `  ${r.chunks} chunks` : `  failed: ${r.error}`);
      } else if (ev.event === 'done') {
        fill.style.width = '100%';
        label.textContent =
          `Indexed ${ev.indexed} file(s), ${ev.chunks} chunks in ${ev.seconds}s` +
          (ev.failed ? `  ·  ${ev.failed} failed` : '');
        toast(`Indexed ${ev.indexed} files (${ev.chunks} chunks)`, 'ok');
        loadSources(); refreshStatus();
      } else if (ev.event === 'error') {
        failure = ev.detail;
      }
    });
    if (failure) throw new Error(failure);
  } catch (e) {
    label.textContent = e.message;
    toast(e.message, 'err');
  } finally {
    $('indexBtn').disabled = false;
    state.brandCore?.set('idle');
  }
}

async function uploadFiles() {
  const input = $('fileInput');
  if (!input.files.length) { toast('Choose files first'); return; }
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  const out = $('uploadOut');
  out.textContent = 'Indexing…';
  state.brandCore?.set('busy');
  try {
    const r = await api('/api/documents/upload', { method: 'POST', body: fd });
    out.innerHTML = '';
    for (const ok of r.indexed) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'tag tag-ok', 'indexed'));
      row.appendChild(el('span', 'grow', `${ok.file} — ${ok.chunks} chunks`));
      out.appendChild(row);
    }
    for (const bad of r.failed) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'tag tag-no', 'failed'));
      row.appendChild(el('span', 'grow', `${bad.file} — ${bad.error}`));
      out.appendChild(row);
    }
    input.value = '';
    loadSources(); refreshStatus();
    if (r.indexed.length) toast(`Indexed ${r.indexed.length} file(s)`, 'ok');
  } catch (e) {
    out.innerHTML = '';
    out.appendChild(el('div', 'dim', e.message));
  } finally {
    state.brandCore?.set('idle');
  }
}

async function loadSources() {
  try {
    const { sources, stats } = await api('/api/documents');
    $('srcCount').textContent = String(stats.sources);
    const list = $('sourceList');
    list.innerHTML = '';
    if (!sources.length) {
      list.appendChild(el('div', 'dim', 'Nothing indexed yet. Add a folder or upload files above.'));
      return;
    }
    for (const s of sources) {
      const row = el('div', 'source-row');
      row.appendChild(el('span', 'ext', s.ext.replace('.', '')));
      const main = el('div', 'source-main');
      main.appendChild(el('div', 'source-name', s.filename));
      main.appendChild(el('div', 'source-meta',
        `${s.chunk_count} chunks · ${fmtBytes(s.bytes)} · ${s.origin} · ${s.path}`));
      row.appendChild(main);

      const acts = el('div', 'source-acts');
      const open = el('button', 'mini', 'open');
      open.addEventListener('click', () => proposeAction('open_file', { path: s.path }));
      const re = el('button', 'mini', 're-index');
      re.addEventListener('click', async () => {
        try {
          await api(`/api/documents/${s.id}/reindex`, { method: 'POST' });
          toast(`Re-indexed ${s.filename}`, 'ok'); loadSources(); refreshStatus();
        } catch (e) { toast(e.message, 'err'); }
      });
      const rm = el('button', 'mini', 'remove');
      rm.addEventListener('click', async () => {
        try {
          await api(`/api/documents/${s.id}`, { method: 'DELETE' });
          toast(`Removed ${s.filename}`, 'ok'); loadSources(); refreshStatus();
        } catch (e) { toast(e.message, 'err'); }
      });
      acts.append(open, re, rm);
      row.appendChild(acts);
      list.appendChild(row);
    }
  } catch (e) { toast(e.message, 'err'); }
}

async function ragSearch() {
  const q = $('ragQuery').value.trim();
  if (!q) return;
  const out = $('ragOut');
  out.textContent = 'Searching locally…';
  try {
    const r = await api('/api/documents/search', {
      method: 'POST', body: JSON.stringify({ query: q, k: 5 }),
    });
    out.innerHTML = '';
    if (!r.results.length) { out.appendChild(el('div', 'dim', 'No passages matched.')); return; }
    const box = el('div', 'sources-box');
    out.appendChild(box);
    renderSources(out, box, r.results);
  } catch (e) {
    out.innerHTML = '';
    out.appendChild(el('div', 'dim', e.message));
  }
}

/* ─────────────────── voice: push-to-talk only ─────────────────── */
async function startRecording() {
  if (state.recording || $('micBtn').disabled) return;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    toast('Microphone access denied: ' + e.message, 'err');
    return;
  }
  state.chunks = [];
  const rec = new MediaRecorder(state.stream);
  rec.ondataavailable = (e) => { if (e.data.size) state.chunks.push(e.data); };
  rec.onstop = handleRecordingStop;
  state.recorder = rec;
  rec.start();
  state.recording = true;

  $('micBtn').classList.add('live');
  $('micHint').textContent = 'Recording… release to transcribe on this Mac.';
  state.core?.set('listen');
  state.brandCore?.set('listen');
  state.wave.start(state.stream);
}

function stopRecording() {
  if (!state.recording) return;
  state.recording = false;
  $('micBtn').classList.remove('live');
  state.wave.stop();
  state.core?.set('idle');
  state.brandCore?.set('idle');
  try { state.recorder.stop(); } catch { /* already stopped */ }
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
}

/* Decode the recording, resample to 16 kHz mono and encode a WAV in the
   page — which is why this app needs no ffmpeg. */
async function blobToWav16k(blob, targetRate = 16000) {
  const buf = await blob.arrayBuffer();
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await ctx.decodeAudioData(buf);
  await ctx.close();

  const target = targetRate;
  const frames = Math.max(Math.ceil(decoded.duration * target), 1);
  const off = new OfflineAudioContext(1, frames, target);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const rendered = await off.startRendering();
  const samples = rendered.getChannelData(0);

  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  const str = (at, s) => { for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, target, true);
  view.setUint32(28, target * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  str(36, 'data'); view.setUint32(40, samples.length * 2, true);
  let p = 44;
  for (let i = 0; i < samples.length; i++, p += 2) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(p, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([view], { type: 'audio/wav' });
}

async function handleRecordingStop() {
  const blob = new Blob(state.chunks, { type: state.recorder.mimeType || 'audio/webm' });
  if (blob.size < 1200) {
    $('micHint').textContent = 'That recording was too short. Hold the button while you speak.';
    return;
  }
  $('micHint').textContent = 'Transcribing on this Mac…';
  try {
    const wav = await blobToWav16k(blob);
    const fd = new FormData();
    fd.append('audio', wav, 'speech.wav');
    const r = await api('/api/stt', { method: 'POST', body: fd });
    if (!r.text) {
      $('micHint').textContent = 'No speech detected. Try again.';
      return;
    }
    $('trText').value = r.text;
    $('trMeta').textContent = `${r.meta.model} · ${r.meta.sample_rate} Hz`;
    $('transcriptReview').hidden = false;
    $('trText').focus();
    $('micHint').textContent = 'Review the transcript before it is sent to the model.';
  } catch (e) {
    toast(e.message, 'err');
    $('micHint').textContent = 'Transcription failed.';
  }
}

function closeReview() {
  $('transcriptReview').hidden = true;
  $('micHint').textContent = 'Hold the microphone to record. Audio is transcribed on this Mac.';
}

/* ───────────────────────── text-to-speech ─────────────────────────
   The persona chooses the voice. While audio is playing the core keeps
   pulsing and a "speaking" pill offers a one-click interrupt, so the reply
   is never something you have to sit through. */
async function speakText(text, btn) {
  if (!text) return;
  if (state.muted) return;      // spoken replies are off until you turn them on
  try {
    if (btn && btn.dataset.on === '1') { await stopSpeaking(); return; }
    if (state.voice) {
      // A cloned voice is synthesised by the GPU worker, so this call takes
      // a few seconds before audio starts.
      $('speakingBtn').hidden = false;
      await api('/api/voice/speak', {
        method: 'POST', body: JSON.stringify({ text, voice: state.voice }),
      });
    } else {
      const p = state.personas[state.persona] || {};
      await api('/api/tts/speak', {
        method: 'POST',
        body: JSON.stringify({ text, voice: p.voice || undefined, rate: p.rate || undefined }),
      });
    }
    if (btn) { btn.dataset.on = '1'; btn.textContent = 'stop'; btn.classList.add('on'); }
    beginSpeechWatch(btn);
  } catch (e) { toast(e.message, 'err'); }
}

async function setMuted(on) {
  state.muted = on;
  try { localStorage.setItem('pai.muted', on ? '1' : '0'); } catch { /* private window */ }
  const b = $('muteBtn');
  b.setAttribute('aria-checked', String(on));
  $('muteLabel').textContent = on ? 'Muted' : 'Voice on';
  try { await api('/api/tts/mute', { method: 'POST', body: JSON.stringify({ muted: on }) }); }
  catch { /* the local guard already applies */ }
  if (on) endSpeechWatch();
}

async function importVoice() {
  const input = $('voiceFile');
  const file = input.files && input.files[0];
  if (!file) { toast('Choose an audio file first'); return; }
  const name = ($('voiceName').value.trim()) || file.name.replace(/\.[^.]+$/, '');
  $('voiceImport').disabled = true;
  $('voiceOut').textContent = 'Converting…';
  try {
    // Decode whatever format it is and re-encode as the 24 kHz mono WAV the
    // cloner expects, so mp3/m4a/aiff all work without ffmpeg.
    const wav = await blobToWav16k(file, 24000);
    const fd = new FormData();
    fd.append('name', name);
    fd.append('audio', wav, 'voice.wav');
    const r = await api('/api/voice', { method: 'POST', body: fd });
    $('voiceOut').textContent = '';
    toast(`Learned “${r.name}” from ${(r.seconds).toFixed(1)}s of audio`, 'ok');
    input.value = '';
    $('voiceName').value = '';
    loadVoice();
    selectVoice(r.name);
  } catch (e) {
    $('voiceOut').textContent = '';
    toast(e.message, 'err');
  } finally {
    $('voiceImport').disabled = false;
  }
}

async function stopSpeaking() {
  try { await api('/api/tts/stop', { method: 'POST' }); } catch { /* already stopped */ }
  endSpeechWatch();
}

/* Poll the server so the interface reflects real playback rather than a
   guess based on word count. */
function beginSpeechWatch(btn) {
  clearInterval(state.speechPoll);
  $('speakingBtn').hidden = false;
  state.core?.set('busy');
  state.brandCore?.set('busy');
  state.speechPoll = setInterval(async () => {
    try {
      const { speaking } = await api('/api/tts/status');
      if (!speaking) endSpeechWatch(btn);
    } catch { endSpeechWatch(btn); }
  }, 500);
}

function endSpeechWatch(btn) {
  clearInterval(state.speechPoll);
  state.speechPoll = null;
  $('speakingBtn').hidden = true;
  if (!state.streaming) { state.core?.set('idle'); state.brandCore?.set('idle'); }
  const target = btn || document.querySelector('.mini[data-on="1"]');
  if (target) { target.dataset.on = '0'; target.textContent = 'speak'; target.classList.remove('on'); }
  document.querySelectorAll('.mini.on').forEach((b) => {
    b.dataset.on = '0'; b.textContent = 'speak'; b.classList.remove('on');
  });
}

/* ───────────────────── actions + approval ───────────────────── */
function showModal(title, build, buttons) {
  $('modalTitle').textContent = title;
  const body = $('modalBody');
  body.innerHTML = '';
  build(body);
  const acts = $('modalActions');
  acts.innerHTML = '';
  for (const b of buttons) {
    const btn = el('button', 'btn ' + (b.cls || ''), b.label);
    btn.addEventListener('click', () => b.onClick(closeModal));
    acts.appendChild(btn);
  }
  $('modalBackdrop').hidden = false;
}
const closeModal = () => { $('modalBackdrop').hidden = true; };

async function proposeAction(kind, params, origin = 'user') {
  let proposal;
  try {
    proposal = await api('/api/actions/propose', {
      method: 'POST', body: JSON.stringify({ kind, params, origin }),
    });
  } catch (e) { toast(e.message, 'err'); return null; }

  if (!proposal.requires_approval) {
    try {
      return await api('/api/actions/execute', {
        method: 'POST', body: JSON.stringify({ token: proposal.token, approved: true }),
      });
    } catch (e) { toast(e.message, 'err'); return null; }
  }

  showModal('Approve this action?', (body) => {
    body.appendChild(el('p', null, proposal.summary));
    const dl = el('dl', 'kv');
    const pair = (k, v) => { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); };
    pair('action', proposal.info.title);
    pair('effect', proposal.info.effect);
    for (const [k, v] of Object.entries(proposal.params)) pair(k, String(v));
    pair('asked by', proposal.origin === 'model-proposal' ? 'the model — verify carefully' : 'you');
    body.appendChild(dl);
    for (const w of (proposal.detail && proposal.detail.warnings) || []) {
      body.appendChild(el('div', 'alert', w));
    }
    if (proposal.origin === 'model-proposal') {
      body.appendChild(el('div', 'alert',
        'Suggested by the model, possibly influenced by document text. Approve only if you recognise it.'));
    }
  }, [
    { label: 'Cancel', cls: 'btn-quiet', onClick: async (close) => {
        close();
        try {
          await api('/api/actions/execute', {
            method: 'POST', body: JSON.stringify({ token: proposal.token, approved: false }),
          });
        } catch { /* token already burned */ }
        loadActions();
      } },
    { label: 'Approve', cls: 'btn-accent', onClick: async (close) => {
        close();
        try {
          const r = await api('/api/actions/execute', {
            method: 'POST', body: JSON.stringify({ token: proposal.token, approved: true }),
          });
          toast(r.executed ? 'Action completed.' : 'Action was not run.', r.executed ? 'ok' : '');
          loadActions();
        } catch (e) { toast(e.message, 'err'); }
      } },
  ]);
  return null;
}

const ACTION_FORMS = {
  search_indexed_files: { field: 'query', ph: 'budget forecast', label: 'Search' },
  open_file:            { field: 'path',  ph: '/full/path/to/indexed.pdf', label: 'Open file' },
  open_url:             { field: 'url',   ph: 'https://example.com', label: 'Open URL' },
  draft_reminder:       { field: 'text',  ph: 'Email the Q3 summary', label: 'Draft' },
};

async function loadActions() {
  try {
    const cat = await api('/api/actions');
    const grid = $('actionCatalog');
    grid.innerHTML = '';

    for (const kind of cat.allowed) {
      const info = cat.info[kind];
      const f = ACTION_FORMS[kind];
      const card = el('div', 'action-card');
      card.appendChild(el('h4', null, info.title));
      card.appendChild(el('div', 'dim', info.effect));
      card.appendChild(el('span',
        'tag ' + (info.requires_approval === 'yes' ? 'tag-warn' : 'tag-ok'),
        info.requires_approval === 'yes' ? 'asks for approval' : 'read-only'));

      const input = el('input');
      input.type = 'text';
      input.placeholder = f.ph;
      card.appendChild(input);
      const out = el('div', 'action-out');

      const run = async () => {
        const v = input.value.trim();
        if (!v) { toast('Enter a value first'); return; }
        const params = {}; params[f.field] = v;
        const r = await proposeAction(kind, params);
        if (kind === 'search_indexed_files' && r && r.result) {
          out.innerHTML = '';
          if (!r.result.results.length) out.appendChild(el('div', 'dim', 'No matches.'));
          for (const hit of r.result.results) {
            const row = el('div', 'row');
            row.appendChild(el('span', 'tag', hit.locator || 'match'));
            row.appendChild(el('span', 'grow', `${hit.filename} — ${hit.snippet}`));
            out.appendChild(row);
          }
        }
        if (kind === 'draft_reminder') { input.value = ''; }
      };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
      const btn = el('button', 'btn btn-accent', f.label);
      btn.addEventListener('click', run);
      card.appendChild(btn);
      card.appendChild(out);
      grid.appendChild(card);
    }

    const rl = $('reminderList');
    rl.innerHTML = '';
    if (!cat.reminders.length) rl.appendChild(el('div', 'dim', 'No drafts yet.'));
    for (const r of cat.reminders) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'tag', r.status));
      row.appendChild(el('span', 'grow', r.text + (r.due ? ` (due ${r.due})` : '')));
      const del = el('button', 'mini', 'delete');
      del.addEventListener('click', async () => {
        await api(`/api/reminders/${r.id}`, { method: 'DELETE' });
        loadActions();
      });
      row.appendChild(del);
      rl.appendChild(row);
    }

    const lg = $('actionLog');
    lg.innerHTML = '';
    if (!cat.log.length) lg.appendChild(el('div', 'dim', 'Nothing logged yet.'));
    for (const e of cat.log) {
      const row = el('div', 'row');
      const cls = e.outcome === 'ok' ? 'tag-ok' : (e.outcome === 'denied' ? 'tag-no' : '');
      row.appendChild(el('span', 'tag ' + cls, e.outcome));
      row.appendChild(el('span', 'grow', `${e.kind} ${JSON.stringify(e.params)}`));
      row.appendChild(el('span', 'dim', fmtTime(e.created_at)));
      lg.appendChild(row);
    }
  } catch (e) { toast(e.message, 'err'); }
}

/* ═════════════════════════ voice studio ═════════════════════════
   Record a sample, name it, and the assistant speaks in that voice. All
   synthesis is local; the picker just chooses which engine speaks. */
async function loadVoice() {
  try {
    const d = await api('/api/voice');
    state.cloneReady = d.available;
    state.voices = d.voices || [];
    $('voiceBadge').textContent = state.voices.length || '';
    $('voiceEngine').textContent = d.available ? 'cloning ready' : 'built-in only';

    const picker = $('voicePicker');
    picker.innerHTML = '';
    const built = el('button', 'voice-opt');
    built.appendChild(el('strong', null, 'Built-in'));
    built.appendChild(el('span', null, 'neural en_GB / say'));
    built.addEventListener('click', () => selectVoice(null));
    picker.appendChild(built);

    for (const v of state.voices) {
      const b = el('button', 'voice-opt');
      b.appendChild(el('strong', null, v.name));
      b.appendChild(el('span', null, `cloned · ${(v.seconds || 0).toFixed(1)}s`));
      b.addEventListener('click', () => selectVoice(v.name));
      picker.appendChild(b);
    }
    markVoice();

    const list = $('voiceList');
    list.innerHTML = '';
    if (!state.voices.length) {
      list.appendChild(el('div', 'dim',
        d.available ? 'No voices yet — record one above.'
                    : 'Voice cloning is not installed. Run ./scripts/install_voice_cloning.sh'));
    }
    for (const v of state.voices) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'tag tag-ok', 'cloned'));
      row.appendChild(el('span', 'grow', `${v.name} — ${(v.seconds || 0).toFixed(1)}s sample`));
      const del = el('button', 'mini', 'delete');
      del.addEventListener('click', async () => {
        await api(`/api/voice/${encodeURIComponent(v.name)}`, { method: 'DELETE' });
        if (state.voice === v.name) selectVoice(null);
        loadVoice();
      });
      row.appendChild(del);
      list.appendChild(row);
    }
  } catch (e) { toast(e.message, 'err'); }
}

function selectVoice(name) {
  state.voice = name;
  try { localStorage.setItem('pai.voice', name || ''); } catch { /* private window */ }
  markVoice();
  toast(name ? `Speaking as “${name}”` : 'Built-in voice');
}

function markVoice() {
  const opts = [...$('voicePicker').children];
  opts.forEach((b, i) => {
    const isBuilt = i === 0;
    const name = isBuilt ? null : state.voices[i - 1]?.name;
    b.setAttribute('aria-checked', String(name === state.voice));
  });
}

async function startVoiceRecording() {
  if (state.recording) return;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false },
    });
  } catch (e) { toast('Microphone denied: ' + e.message, 'err'); return; }

  state.chunks = [];
  const rec = new MediaRecorder(state.stream);
  rec.ondataavailable = (e) => { if (e.data.size) state.chunks.push(e.data); };
  rec.onstop = finishVoiceRecording;
  state.recorder = rec;
  rec.start();
  state.recording = true;
  state.recStart = performance.now();

  $('voiceRecBtn').classList.add('live');
  $('voiceRecHint').textContent = 'Recording — speak naturally';
  state.recWave = state.recWave || new Waveform($('voiceWave'));
  state.recWave.start(state.stream);
  state.recTimer = setInterval(() => {
    $('voiceTimer').textContent = ((performance.now() - state.recStart) / 1000).toFixed(1) + 's';
  }, 100);
}

function stopVoiceRecording() {
  if (!state.recording) return;
  state.recording = false;
  clearInterval(state.recTimer);
  $('voiceRecBtn').classList.remove('live');
  state.recWave?.stop();
  try { state.recorder.stop(); } catch { /* already stopped */ }
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
}

async function finishVoiceRecording() {
  const seconds = (performance.now() - state.recStart) / 1000;
  const blob = new Blob(state.chunks, { type: state.recorder.mimeType || 'audio/webm' });
  if (seconds < 4) {
    $('voiceRecHint').textContent = 'Too short — hold for at least 4 seconds.';
    return;
  }
  $('voiceRecHint').textContent = 'Converting…';
  try {
    state.recBlob = await blobToWav16k(blob, 24000);
    $('voiceRecHint').textContent = `Captured ${seconds.toFixed(1)}s — name it and save.`;
    $('voiceSave').disabled = false;
    $('voiceName').focus();
  } catch (e) {
    $('voiceRecHint').textContent = 'Could not process that recording.';
    toast(e.message, 'err');
  }
}

async function saveVoice() {
  const name = $('voiceName').value.trim();
  if (!name) { toast('Name the voice first'); return; }
  if (!state.recBlob) { toast('Record a sample first'); return; }
  const fd = new FormData();
  fd.append('name', name);
  fd.append('audio', state.recBlob, 'voice.wav');
  $('voiceSave').disabled = true;
  try {
    const r = await api('/api/voice', { method: 'POST', body: fd });
    toast(`Saved “${r.name}” (${r.seconds}s)`, 'ok');
    $('voiceName').value = '';
    state.recBlob = null;
    $('voiceOut').textContent = '';
    loadVoice();
    selectVoice(r.name);
  } catch (e) {
    toast(e.message, 'err');
    $('voiceSave').disabled = false;
  }
}

async function testVoice() {
  const text = $('voiceTest').value.trim() ||
    'All systems are local, sir. Nothing leaves this machine.';
  $('voiceTestBtn').disabled = true;
  try { await speakText(text); }
  finally { $('voiceTestBtn').disabled = false; }
}

/* ═══════════════════════════ automation ═══════════════════════════
   The model gets real tools. `state.autonomy` decides how much it may do
   before asking — the gate is enforced on the server; this is just its
   control surface. */
const LEVEL_COPY = {
  off:  ['Off', 'Every action waits for your click. Safest with untrusted documents indexed.'],
  safe: ['Safe', 'Read-only actions run on their own. Writes and deletions still ask.'],
  full: ['Full', 'Everything runs unattended, including shell and deletions.'],
};

async function loadAutomation() {
  try {
    const d = await api('/api/power');
    state.autonomy = d.autonomy || 'off';
    renderLevels(d.autonomy_levels || ['off', 'safe', 'full']);

    const list = $('toolList');
    list.innerHTML = '';
    const entries = Object.entries(d.tools);
    $('toolCount').textContent = String(entries.length);
    for (const [name, t] of entries) {
      const row = el('div', 'tool-row');
      const tag = el('span', 'tag', t.risk);
      tag.classList.add(t.risk === 'read' ? 'tag-ok'
        : t.risk === 'destructive' ? 'tag-no' : 'tag-warn');
      row.appendChild(tag);
      const code = el('code', null, name);
      row.appendChild(code);
      row.appendChild(el('span', 'grow dim', t.summary));
      list.appendChild(row);
    }
  } catch (e) { toast(e.message, 'err'); }
}

function renderLevels(levels) {
  const box = $('autonomyLevels');
  box.innerHTML = '';
  for (const lv of levels) {
    const [label, blurb] = LEVEL_COPY[lv] || [lv, ''];
    const b = el('button', 'level');
    b.dataset.level = lv;
    b.setAttribute('role', 'radio');
    b.appendChild(el('strong', null, label));
    b.appendChild(el('span', null, blurb));
    b.addEventListener('click', () => setAutonomy(lv));
    box.appendChild(b);
  }
  markAutonomy();
}

function markAutonomy() {
  [...$('autonomyLevels').children].forEach((b) =>
    b.setAttribute('aria-checked', String(b.dataset.level === state.autonomy)));
  $('autonomyBadge').textContent = state.autonomy === 'off' ? '' : state.autonomy;
  const [, blurb] = LEVEL_COPY[state.autonomy] || ['', ''];
  $('autonomyHint').textContent = blurb;
}

async function setAutonomy(level) {
  const apply = async () => {
    try {
      await api('/api/power/autonomy', {
        method: 'POST', body: JSON.stringify({ level }),
      });
      state.autonomy = level;
      markAutonomy();
      toast(`Autonomy: ${level}`, level === 'full' ? 'err' : 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };

  // 'full' means the model can delete files and run shell commands with no
  // further prompting. That deserves one deliberate confirmation.
  if (level === 'full') {
    showModal('Hand over full control?', (body) => {
      body.appendChild(el('p', null,
        'At Full autonomy the model runs every action immediately — including ' +
        'an unrestricted shell, AppleScript, and deleting files — without asking you first.'));
      body.appendChild(el('div', 'alert',
        'This app also reads documents you did not write. A document containing ' +
        'instructions reaches the model like any other text. Prefer Safe while ' +
        'untrusted files are indexed.'));
      body.appendChild(el('p', 'dim', 'You can stop any run mid-flight with the Stop button.'));
    }, [
      { label: 'Cancel', cls: 'btn-quiet', onClick: (c) => c() },
      { label: 'Enable full control', cls: 'btn-danger',
        onClick: (c) => { c(); apply(); } },
    ]);
    return;
  }
  apply();
}

function evRow(tag, kind, text, pre, lane) {
  const row = el('div', `ev ev-${kind}`);
  if (lane != null) row.dataset.lane = String(lane);
  row.appendChild(el('span', 'ev-tag', tag));
  const body = el('div', 'ev-body');
  if (text) body.appendChild(el('div', null, text));
  if (pre) {
    const p = el('pre', null, typeof pre === 'string' ? pre : JSON.stringify(pre, null, 1));
    body.appendChild(p);
  }
  row.appendChild(body);
  $('agentStream').appendChild(row);
  row.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'nearest' });
  return row;
}

async function loadMemory() {
  try {
    const d = await api('/api/memory');
    $('memCount').textContent = String(d.count);
    $('learnToggle').setAttribute('aria-checked', String(d.learning));
    const list = $('memList');
    list.innerHTML = '';
    if (!d.memories.length) {
      list.appendChild(el('div', 'dim',
        d.learning ? 'Nothing learned yet — it picks things up as you talk.'
                   : 'Learning is off.'));
      return;
    }
    for (const m of d.memories) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'tag ' + (m.kind === 'learned' ? 'tag-ok' : ''), m.kind));
      row.appendChild(el('span', 'grow', m.text));
      if (m.uses) row.appendChild(el('span', 'dim', `used ${m.uses}\u00d7`));
      const del = el('button', 'mini', 'forget');
      del.addEventListener('click', async () => {
        await api(`/api/memory/${m.id}`, { method: 'DELETE' });
        loadMemory();
      });
      row.appendChild(del);
      list.appendChild(row);
    }
  } catch (e) { toast(e.message, 'err'); }
}

async function runAgent() {
  const task = $('agentTask').value.trim();
  if (!task) { toast('Describe a task first'); return; }
  $('agentStream').innerHTML = '';
  $('agentRun').disabled = true;
  $('agentStop').hidden = false;
  state.brandCore?.set('busy');

  try {
    const extra = [$('agentTask2')?.value.trim(), $('agentTask3')?.value.trim()]
      .filter(Boolean);
    const lanes = state.modes[state.mode]?.lanes || 1;
    const parallel = lanes > 1 && extra.length > 0;

    const res = await fetch(parallel ? '/api/agent/parallel' : '/api/agent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(parallel
        ? { tasks: [task, ...extra].slice(0, lanes), autonomy: state.autonomy, mode: state.mode }
        : { task, autonomy: state.autonomy, model: state.modes[state.mode]?.model }),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.detail || `Failed (${res.status})`);
    }
    await readSSE(res, (ev) => {
      if (ev.type === 'parallel_start') {
        evRow('lanes', 'final', `Running ${ev.lanes} tasks at once`);
      } else if (ev.type === 'lane_done') {
        evRow('lane', 'read', `Lane ${ev.lane} finished`, null, ev.lane);
      } else if (ev.type === 'parallel_done') {
        evRow('done', 'final', 'All lanes finished');
      } else if (ev.type === 'run') {
        state.agentRun = ev.run_id;
        evRow('run', 'read', `${ev.tools} tools available · autonomy ${ev.autonomy}`, null, ev.lane);
      } else if (ev.type === 'thinking') {
        evRow('step', 'read', `Step ${ev.step} — deciding what to do`, null, ev.lane);
      } else if (ev.type === 'note') {
        evRow('says', 'read', ev.text, null, ev.lane);
      } else if (ev.type === 'tool_call') {
        const row = evRow(ev.tool, ev.risk, null, ev.args, ev.lane);
        if (ev.needs_approval) {
          const acts = el('div', 'ev-actions');
          const yes = el('button', 'btn btn-accent', 'Approve');
          const no = el('button', 'btn btn-quiet', 'Deny');
          const decide = async (approved) => {
            acts.remove();
            try {
              await api('/api/agent/approve', {
                method: 'POST',
                body: JSON.stringify({ run_id: state.agentRun, call_id: ev.id, approved }),
              });
            } catch (e) { toast(e.message, 'err'); }
          };
          yes.addEventListener('click', () => decide(true));
          no.addEventListener('click', () => decide(false));
          acts.append(yes, no);
          row.querySelector('.ev-body').appendChild(acts);
        }
      } else if (ev.type === 'verdict') {
        evRow(ev.approved ? 'approved' : 'denied', ev.approved ? 'read' : 'err', '');
      } else if (ev.type === 'tool_result') {
        evRow('result', 'read', null, ev.result, ev.lane);
      } else if (ev.type === 'tool_error') {
        evRow('failed', 'err', `${ev.tool}: ${ev.detail}`, null, ev.lane);
      } else if (ev.type === 'final') {
        evRow('done', 'final', ev.text, null, ev.lane);
        if (state.personas[state.persona]?.speak_by_default) speakText(ev.text);
      } else if (ev.type === 'cancelled') {
        evRow('stopped', 'err', 'Run cancelled.');
      } else if (ev.type === 'error') {
        evRow('error', 'err', ev.detail);
      }
    });
  } catch (e) {
    evRow('error', 'err', e.message);
  } finally {
    $('agentRun').disabled = false;
    $('agentStop').hidden = true;
    state.agentRun = null;
    if (!state.streaming) state.brandCore?.set('idle');
    loadActions();
  }
}

async function stopAgent() {
  if (!state.agentRun) return;
  try { await api(`/api/agent/${state.agentRun}/cancel`, { method: 'POST' }); }
  catch (e) { toast(e.message, 'err'); }
}

/* ───────────────────────── privacy ───────────────────────── */
async function loadPrivacy() {
  try {
    const p = await api('/api/privacy');
    const ul = $('guaranteeList');
    ul.innerHTML = '';
    for (const g of p.guarantees) ul.appendChild(el('li', null, g));
    $('dataDir').textContent = p.data_dir;
    $('dataSummary').textContent = `${p.file_count} files · ${fmtBytes(p.total_bytes)} total`;
    const box = $('dataFiles');
    box.innerHTML = '';
    for (const f of p.files.slice(0, 40)) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'grow mono', f.path));
      row.appendChild(el('span', 'dim', fmtBytes(f.bytes)));
      box.appendChild(row);
    }
    if (p.file_count > 40) box.appendChild(el('div', 'dim', `and ${p.file_count - 40} more`));
    state.deletePhrase = p.delete_phrase;
  } catch (e) { toast(e.message, 'err'); }
}

function confirmWipe() {
  const phrase = state.deletePhrase;
  let field;
  showModal('Delete all local data?', (body) => {
    body.appendChild(el('p', null,
      'This removes every thread, message, indexed document, embedding, reminder draft, audit entry and uploaded copy. It cannot be undone.'));
    body.appendChild(el('div', 'alert',
      'Downloaded model weights are kept. Remove those with "ollama rm" if you want them gone too.'));
    body.appendChild(el('p', 'dim', `Type ${phrase} to confirm:`));
    field = el('input');
    field.type = 'text';
    field.placeholder = phrase;
    body.appendChild(field);
    setTimeout(() => field.focus(), 60);
  }, [
    { label: 'Cancel', cls: 'btn-quiet', onClick: (close) => close() },
    { label: 'Delete everything', cls: 'btn-danger', onClick: async (close) => {
        try {
          await api('/api/privacy/delete_all', {
            method: 'POST', body: JSON.stringify({ confirm_phrase: field.value }),
          });
          close();
          toast('All local data deleted.', 'ok');
          newThread(); loadSources(); loadPrivacy(); loadActions();
          refreshStatus(); refreshVitals();
        } catch (e) { toast(e.message, 'err'); }
      } },
  ]);
}

/* ═══════════════════════ command palette ═══════════════════════ */
const COMMANDS = [
  { icon: '1', label: 'Go to Chat',            hint: '1', run: () => switchView('chat') },
  { icon: '2', label: 'Go to Documents',       hint: '2', run: () => switchView('documents') },
  { icon: '3', label: 'Go to Build',           hint: '3', run: () => switchView('code') },
  { icon: '4', label: 'Go to Automate',        hint: '4', run: () => switchView('automate') },
  { icon: '5', label: 'Go to Connect',         hint: '5', run: () => switchView('connect') },
  { icon: '6', label: 'Go to Watch',           hint: '6', run: () => switchView('watch') },
  { icon: '8', label: 'Go to Privacy',         hint: '8', run: () => switchView('privacy') },
  { icon: '◉', label: 'Check every watch now', run: () => { switchView('watch'); runAllWatches(); } },
  { icon: '⚑', label: 'Add a watch…',          run: () => { switchView('watch'); newWatch(); } },
  { icon: '+', label: 'New thread',            hint: '⌘N', run: () => { switchView('chat'); newThread(); } },
  { icon: '⌥', label: 'Toggle "Use documents"', run: () => { setUseDocs(!state.useDocs); toast(`Documents ${state.useDocs ? 'on' : 'off'}`); } },
  { icon: '↻', label: 'Re-index all documents', run: async () => {
      toast('Re-indexing…');
      try {
        const r = await api('/api/documents/reindex_all', { method: 'POST' });
        toast(`Re-indexed ${r.reindexed} source(s)`, 'ok'); loadSources(); refreshStatus();
      } catch (e) { toast(e.message, 'err'); }
    } },
  { icon: '⏹', label: 'Stop speaking', run: async () => {
      await api('/api/tts/stop', { method: 'POST' }); toast('Stopped.');
    } },
  { icon: '⌫', label: 'Delete all local data…', run: () => { switchView('privacy'); confirmWipe(); } },
];

let paletteSel = 0;
let paletteItems = [];

function openPalette() {
  $('paletteBackdrop').hidden = false;
  const input = $('paletteInput');
  input.value = '';
  renderPalette('');
  setTimeout(() => input.focus(), 30);
}
const closePalette = () => {
  $('paletteBackdrop').hidden = true;
  // Release focus, otherwise the number shortcuts stay suppressed by
  // the "don't hijack keys while typing" guard.
  $('paletteInput').blur();
};

function renderPalette(query) {
  const q = query.toLowerCase().trim();
  const models = (state.status?.runtime.models || [])
    .filter((m) => !m.includes('embed'))
    .map((m) => ({
      icon: '◆', label: `Switch model to ${m}`, hint: 'model',
      run: () => { $('modelSelect').value = m; toast(`Model: ${m}`); },
    }));
  const all = COMMANDS.concat(models);
  paletteItems = q ? all.filter((c) => c.label.toLowerCase().includes(q)) : all;
  paletteSel = 0;

  const list = $('paletteList');
  list.innerHTML = '';
  if (!paletteItems.length) {
    list.appendChild(el('div', 'p-empty', 'No matching command.'));
    return;
  }
  paletteItems.forEach((c, i) => {
    const row = el('button', 'p-item' + (i === 0 ? ' sel' : ''));
    row.appendChild(el('span', 'p-icon', c.icon));
    row.appendChild(el('span', 'p-label', c.label));
    if (c.hint) row.appendChild(el('span', 'p-hint', c.hint));
    row.addEventListener('click', () => { closePalette(); c.run(); });
    row.addEventListener('mouseenter', () => {
      paletteSel = i;
      [...list.children].forEach((n, j) => n.classList.toggle('sel', j === i));
    });
    list.appendChild(row);
  });
}

function movePalette(delta) {
  const list = $('paletteList');
  if (!paletteItems.length) return;
  paletteSel = (paletteSel + delta + paletteItems.length) % paletteItems.length;
  [...list.children].forEach((n, j) => n.classList.toggle('sel', j === paletteSel));
  list.children[paletteSel]?.scrollIntoView({ block: 'nearest' });
}

/* ───────────────────────── navigation ───────────────────────── */
const TITLES = {
  chat: 'Chat', documents: 'Documents', code: 'Build', automate: 'Automate',
  connect: 'Connect', watch: 'Watch', voice: 'Voice',
  actions: 'Allowlisted actions', privacy: 'Privacy',
};

/*
 * Slide the section marker to the active item.
 *
 * Measuring here rather than styling `.nav-item.active` directly is what buys
 * the movement: one element travels between positions, so the eye follows it
 * instead of noticing that one highlight vanished and another appeared.
 */
function moveNavIndicator() {
  const nav = document.querySelector('.nav');
  const active = nav?.querySelector('.nav-item.active');
  if (!nav || !active) return;
  nav.style.setProperty('--nav-y', `${active.offsetTop}px`);
  nav.style.setProperty('--nav-h', `${active.offsetHeight}px`);
  nav.style.setProperty('--nav-o', '1');
}

function switchView(name) {
  document.querySelectorAll('.nav-item').forEach((t) =>
    t.classList.toggle('active', t.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) =>
    v.classList.toggle('active', v.id === 'view-' + name));
  $('viewTitle').textContent = TITLES[name] || name;
  // retunes every accent in the chrome to this subsystem's hue — on the root
  // as well, so overlays outside .app (modal, palette, toasts) follow too
  $('app').dataset.view = name;
  document.documentElement.dataset.view = name;
  $('threadsBlock').hidden = name !== 'chat';
  moveNavIndicator();
  if (name === 'documents') loadSources();
  if (name === 'automate') { loadAutomation(); loadMemory(); loadSkills(); }
  if (name === 'voice') loadVoice();
  if (name === 'actions') loadActions();
  if (name === 'privacy') loadPrivacy();
  if (name === 'connect') loadConnectors();
  if (name === 'code') loadCodeView();
  // The watch view polls while you are looking at it and stops when you leave;
  // a background timer nobody is watching is just heat.
  if (name === 'watch') { loadWatch(); startWatchPolling(); } else stopWatchPolling();
  closeRail();
}

function openRail() {
  $('rail').classList.add('open');
  if (!$('railScrim')) {
    const scrim = el('div', 'scrim');
    scrim.id = 'railScrim';
    scrim.addEventListener('click', closeRail);
    document.body.appendChild(scrim);
  }
}
function closeRail() {
  $('rail').classList.remove('open');
  $('railScrim')?.remove();
}

/* ───────────────────────── wiring ───────────────────────── */
function init() {
  state.brandCore = new Core($('brandCore'), { rings: 2, base: 0.42 });
  state.core = new Core($('heroCore'), { rings: 3, base: 0.3 });
  state.wave = new Waveform($('micWave'));

  document.querySelectorAll('.nav-item').forEach((t) =>
    t.addEventListener('click', () => switchView(t.dataset.view)));
  $('menuBtn').addEventListener('click', () =>
    ($('rail').classList.contains('open') ? closeRail() : openRail()));
  $('newChatBtn').addEventListener('click', newThread);
  $('sendBtn').addEventListener('click', () => sendMessage());
  $('cmdBtn').addEventListener('click', openPalette);

  const input = $('input');
  const autosize = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  };
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  $('useDocsBtn').addEventListener('click', () => setUseDocs(!state.useDocs));
  $('useWatchBtn').addEventListener('click', () => setUseWatch(!state.useWatch));

  // Connect
  $('egressToggle').addEventListener('click', toggleEgress);
  $('egressSwitch').addEventListener('click', (e) => {
    if (e.target.closest('#egressToggle')) return;
  });

  // Watch
  $('watchNew').addEventListener('click', () => newWatch());
  $('watchRunAll').addEventListener('click', runAllWatches);
  $('eventsSeen').addEventListener('click', async () => {
    await api('/api/watch/events/seen', { method: 'POST', body: '{}' });
    loadWatch();
  });

  // Build
  $('codeScan').addEventListener('click', scanProject);
  $('codeRun').addEventListener('click', runBuild);
  $('codeStop').addEventListener('click', stopBuild);
  $('codePath').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); scanProject(); }
  });

  // Push-to-talk: hold. Never always-listening.
  const mic = $('micBtn');
  mic.addEventListener('mousedown', startRecording);
  mic.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); }, { passive: false });
  ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach((ev) =>
    mic.addEventListener(ev, stopRecording));
  mic.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !state.recording) { e.preventDefault(); startRecording(); }
  });
  mic.addEventListener('keyup', (e) => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); stopRecording(); }
  });

  $('trUse').addEventListener('click', () => {
    $('input').value = $('trText').value.trim();
    closeReview();
    $('input').focus();
    autosize();
  });
  $('trRetry').addEventListener('click', () => { closeReview(); startRecording(); });
  $('trDiscard').addEventListener('click', closeReview);

  $('previewBtn').addEventListener('click', previewFolder);
  $('indexBtn').addEventListener('click', indexFolder);
  $('uploadBtn').addEventListener('click', uploadFiles);
  $('ragBtn').addEventListener('click', ragSearch);
  $('ragQuery').addEventListener('keydown', (e) => { if (e.key === 'Enter') ragSearch(); });
  $('folderPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') previewFolder(); });

  $('reindexAllBtn').addEventListener('click', async () => {
    toast('Re-indexing…');
    try {
      const r = await api('/api/documents/reindex_all', { method: 'POST' });
      toast(`Re-indexed ${r.reindexed} source(s)`, 'ok');
      loadSources(); refreshStatus();
    } catch (e) { toast(e.message, 'err'); }
  });

  $('clearDocsBtn').addEventListener('click', () => {
    showModal('Delete all indexed data?', (body) => {
      body.appendChild(el('p', null,
        'Removes every indexed source, chunk and embedding, plus uploaded copies. Your original files on disk are not touched.'));
    }, [
      { label: 'Cancel', cls: 'btn-quiet', onClick: (c) => c() },
      { label: 'Delete index', cls: 'btn-danger', onClick: async (c) => {
          try {
            await api('/api/documents', { method: 'DELETE' });
            c(); toast('Index cleared.', 'ok'); loadSources(); refreshStatus();
          } catch (e) { toast(e.message, 'err'); }
        } },
    ]);
  });

  $('speakingBtn').addEventListener('click', stopSpeaking);
  $('muteBtn').addEventListener('click', () => setMuted(!state.muted));
  $('voiceImport').addEventListener('click', importVoice);
  const rec = $('voiceRecBtn');
  rec.addEventListener('mousedown', startVoiceRecording);
  rec.addEventListener('touchstart', (e) => { e.preventDefault(); startVoiceRecording(); }, { passive: false });
  ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach((ev) =>
    rec.addEventListener(ev, stopVoiceRecording));
  $('voiceSave').addEventListener('click', saveVoice);
  $('voiceTestBtn').addEventListener('click', testVoice);
  $('voiceTest').addEventListener('keydown', (e) => { if (e.key === 'Enter') testVoice(); });

  $('learnToggle').addEventListener('click', async () => {
    const on = $('learnToggle').getAttribute('aria-checked') !== 'true';
    await api('/api/memory/learning', {
      method: 'POST', body: JSON.stringify({ enabled: on }),
    });
    toast(on ? 'Learning on' : 'Learning off');
    loadMemory();
  });
  $('memAdd').addEventListener('click', async () => {
    const text = $('memInput').value.trim();
    if (!text) return;
    try {
      const r = await api('/api/memory', { method: 'POST', body: JSON.stringify({ text }) });
      toast(r.stored ? 'Remembered.' : 'Already knew something like that.', 'ok');
      $('memInput').value = '';
      loadMemory();
    } catch (e) { toast(e.message, 'err'); }
  });
  $('memInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('memAdd').click(); });
  $('memClear').addEventListener('click', () => {
    showModal('Forget everything it has learned?', (body) => {
      body.appendChild(el('p', null,
        'Removes every fact learned from your conversations. Your chats stay.'));
    }, [
      { label: 'Cancel', cls: 'btn-quiet', onClick: (c) => c() },
      { label: 'Forget all', cls: 'btn-danger', onClick: async (c) => {
          await api('/api/memory', { method: 'DELETE' });
          c(); toast('Memory cleared.', 'ok'); loadMemory();
        } },
    ]);
  });

  $('modeSelect').addEventListener('change', (e) => {
    state.mode = e.target.value;
    applyMode();
    const m = state.modes[state.mode];
    toast(`${state.mode} — ${m?.blurb || ''}`);
  });
  $('skillNew').addEventListener('click', newSkill);
  $('agentRun').addEventListener('click', runAgent);
  $('agentStop').addEventListener('click', stopAgent);
  $('agentTask').addEventListener('keydown', (e) => { if (e.key === 'Enter') runAgent(); });

  // Sent by the app's menu bar item (native shell).
  window.addEventListener('pai:new-thread', () => { switchView('chat'); newThread(); });

  $('wipeBtn').addEventListener('click', confirmWipe);

  $('modalBackdrop').addEventListener('click', (e) => {
    if (e.target === $('modalBackdrop')) closeModal();
  });
  $('paletteBackdrop').addEventListener('click', (e) => {
    if (e.target === $('paletteBackdrop')) closePalette();
  });
  $('paletteInput').addEventListener('input', (e) => renderPalette(e.target.value));
  $('paletteInput').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); movePalette(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); movePalette(-1); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const cmd = paletteItems[paletteSel];
      if (cmd) { closePalette(); cmd.run(); }
    }
  });

  // global shortcuts
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      $('paletteBackdrop').hidden ? openPalette() : closePalette();
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n') {
      e.preventDefault(); switchView('chat'); newThread(); return;
    }
    if (e.key === 'Escape') {
      if (!$('paletteBackdrop').hidden) closePalette();
      else if (!$('modalBackdrop').hidden) closeModal();
      else closeRail();
      return;
    }
    if (!typing && !e.metaKey && !e.ctrlKey && /^[1-8]$/.test(e.key)) {
      switchView(['chat', 'documents', 'code', 'automate', 'connect',
                  'watch', 'voice', 'privacy'][Number(e.key) - 1]);
    }
  });

  renderStarters();
  loadNames().then(loadPersonas).then(loadModes);
  loadAutomation();
  loadVoice();
  try { setMuted(localStorage.getItem('pai.muted') === '1'); } catch { setMuted(false); }
  try { state.voice = localStorage.getItem('pai.voice') || null; } catch { state.voice = null; }
  // status first: vitals fall back to the configured model name, which is
  // only known once status has landed.
  refreshStatus().then(refreshVitals);
  loadThreads();
  moveNavIndicator();
  window.addEventListener('resize', moveNavIndicator);
  pollWatchBadge();
  setInterval(refreshStatus, 20000);
  setInterval(refreshVitals, 4000);
  setInterval(pollWatchBadge, 60000);
}

document.addEventListener('DOMContentLoaded', init);

/* ═══════════════════════════════════════════════════════════════════════
   CONNECT — every bridge, grouped by where it actually reaches
   ═══════════════════════════════════════════════════════════════════════ */

const RISK_TITLE = {
  read: 'reads only', write: 'changes something',
  destructive: 'irreversible', system: 'system surface',
};

async function loadConnectors() {
  const host = $('connectorList');
  try {
    const data = await api('/api/connectors');
    state.egress = data.egress;
    renderEgress(data.egress);
    host.innerHTML = '';

    let localTools = 0;
    for (const cat of data.categories) {
      const section = el('section', 'cat');
      const head = el('div', 'cat-head');
      head.appendChild(el('h3', null, cat.label));
      head.appendChild(el('span', `reach ${cat.reach}`,
        cat.reach === 'local' ? 'stays on this Mac' : 'leaves this Mac'));
      head.appendChild(el('p', null, cat.blurb));
      section.appendChild(head);

      const grid = el('div', 'cat-grid');
      for (const entry of cat.apps) {
        grid.appendChild(cat.id === 'dev'
          ? serviceCard(entry.service)
          : appCard(entry));
        if (cat.reach === 'local') localTools += entry.tools.length;
      }
      section.appendChild(grid);
      host.appendChild(section);
    }
    $('connectBadge').textContent = String(localTools);
  } catch (e) {
    host.innerHTML = '';
    host.appendChild(el('p', 'dim', e.message));
  }
}

function appCard(entry) {
  const card = el('div', 'conn-card');
  card.appendChild(el('h4', null, entry.app));
  for (const tool of entry.tools) {
    const row = el('div', 'tool');
    const code = el('code', null, tool.name);
    code.title = `${tool.name} — ${RISK_TITLE[tool.risk] || tool.risk}`;
    row.appendChild(code);
    row.appendChild(el('span', null, tool.summary));
    card.appendChild(row);
  }
  return card;
}

function serviceCard(service) {
  const card = el('div', 'conn-card');
  const title = el('h4', null, '');
  title.appendChild(el('span', `dot ${service.token_set ? 'ok' : ''}`));
  title.appendChild(document.createTextNode(service.label));
  card.appendChild(title);
  card.appendChild(el('p', 'dim', service.blurb));

  const meta = el('div', 'tool');
  meta.appendChild(el('code', null, service.hosts.join(', ') || 'the URL you give it'));
  card.appendChild(meta);

  const actions = el('div', 'inline');
  actions.style.marginTop = '8px';

  if (service.needs_token) {
    const label = service.token_set ? `Token ${service.token_hint}` : 'Add token';
    const btn = el('button', 'btn' + (service.token_set ? '' : ' btn-accent'), label);
    btn.addEventListener('click', () => tokenModal(service));
    actions.appendChild(btn);
  }
  const watchBtn = el('button', 'btn', 'Watch this');
  watchBtn.addEventListener('click', () => newWatch(service.id));
  actions.appendChild(watchBtn);
  card.appendChild(actions);
  return card;
}

function tokenModal(service) {
  showModal(`${service.label} token`, (body) => {
    body.appendChild(el('p', 'dim',
      `Stored locally in your data folder and sent only to ${service.hosts.join(', ')}. ` +
      'It is never shown again after you save it, and never given to the model.'));
    if (service.token_url) {
      const link = el('p', 'dim', '');
      const a = el('a', null, service.token_url);
      a.href = service.token_url;
      a.target = '_blank';
      a.rel = 'noreferrer';
      link.appendChild(document.createTextNode('Create one at '));
      link.appendChild(a);
      body.appendChild(link);
    }
    const field = el('div', 'form-field');
    field.appendChild(el('label', null, service.token_label));
    const input = el('input');
    input.type = 'password';
    input.id = 'tokenInput';
    input.autocomplete = 'off';
    input.spellcheck = false;
    field.appendChild(input);
    body.appendChild(field);
    setTimeout(() => input.focus(), 50);
  }, [
    ...(service.token_set ? [{
      label: 'Remove', cls: 'btn-danger', onClick: async (close) => {
        await api(`/api/egress/token/${service.id}`, { method: 'DELETE' });
        close(); toast(`${service.label} token removed`); loadConnectors();
      },
    }] : []),
    { label: 'Cancel', cls: 'btn-quiet', onClick: (close) => close() },
    {
      label: 'Save', cls: 'btn-accent', onClick: async (close) => {
        const token = $('tokenInput').value.trim();
        if (!token) { toast('Paste the token first', 'err'); return; }
        try {
          await api('/api/egress/token', {
            method: 'POST',
            body: JSON.stringify({ provider: service.id, token }),
          });
          close(); toast(`${service.label} token saved`); loadConnectors();
        } catch (e) { toast(e.message, 'err'); }
      },
    },
  ]);
}

function renderEgress(summary) {
  const box = $('egressSwitch');
  const on = !!summary.enabled;
  box.classList.toggle('off', !on);
  $('egressDot').className = `dot ${on ? 'building' : ''}`;
  $('egressToggle').setAttribute('aria-checked', String(on));
  $('egressBlurb').textContent = on
    ? `On. ${summary.requests} request${summary.requests === 1 ? '' : 's'} made so far` +
      (summary.hosts.length ? `, to ${summary.hosts.join(', ')}.` : '.') +
      ' Only watcher status polls go out — never your prompts, documents or conversations.'
    : 'Off. Nothing in this app makes an outbound request. The model, your ' +
      'documents and your conversations stay local either way — this switch ' +
      'only governs status polls to the services below.';

  const log = $('egressLog');
  log.hidden = !on || !summary.requests;
  if (!log.hidden) {
    log.innerHTML = '';
    api('/api/egress').then(({ recent }) => {
      log.innerHTML = '';
      const head = el('div', 'panel-head');
      head.appendChild(el('h3', null, 'Everything that left this Mac'));
      log.appendChild(head);
      for (const r of recent.slice(0, 12)) {
        const row = el('div', 'feed-row');
        row.appendChild(el('span', 'src', r.provider));
        const body = el('div');
        body.appendChild(el('b', null, `${r.host}${r.path}`));
        body.appendChild(el('span', 'detail',
          `${r.status ?? 'no response'} · ${fmtBytes(r.bytes)} · ${r.detail || ''}`));
        row.appendChild(body);
        row.appendChild(el('time', null, fmtTime(r.created_at)));
        log.appendChild(row);
      }
    }).catch(() => {});
  }
}

async function toggleEgress() {
  const next = !(state.egress?.enabled);
  if (next && !confirm(
    'Turn on outbound network access?\n\n' +
    'Watchers will be able to reach the deployment and CI services you have ' +
    'given tokens for. Your prompts, documents, conversations and the model ' +
    'itself remain entirely local — this only allows status polls.')) return;
  try {
    const d = await api('/api/egress/enable', {
      method: 'POST', body: JSON.stringify({ enabled: next }),
    });
    state.egress = d.summary;
    renderEgress(d.summary);
    toast(next ? 'Outbound access on — watchers can reach their services'
               : 'Outbound access off — nothing leaves this Mac');
  } catch (e) { toast(e.message, 'err'); }
}

/* ═══════════════════════════════════════════════════════════════════════
   WATCH — recurring polls, and only what changed
   ═══════════════════════════════════════════════════════════════════════ */

const STATE_WORD = {
  ok: 'healthy', building: 'in progress', failed: 'failed',
  stopped: 'stopped', new: 'new', unknown: 'unknown',
};

function worstState(items) {
  if (!items?.length) return 'idle';
  for (const s of ['failed', 'building', 'new', 'ok']) {
    if (items.some((i) => i.state === s)) return s;
  }
  return 'idle';
}

async function loadWatch() {
  try {
    const data = await api('/api/watch');
    state.egress = data.egress;
    renderWatchers(data.watchers, data.status);
    renderEvents(data.events);
    updateWatchBadge(data.status.unseen);
  } catch (e) { toast(e.message, 'err'); }
}

function updateWatchBadge(unseen) {
  const badge = $('watchBadge');
  const toggle = $('watchToggleCount');
  badge.textContent = unseen ? String(unseen) : '';
  if (toggle) toggle.textContent = unseen ? String(unseen) : '';
}

function renderWatchers(list, status) {
  $('watchCount').textContent = String(list.length);
  $('watchStatus').textContent = status.running
    ? `Scheduler running · ${status.enabled} of ${status.count} active` +
      (status.network ? ` · ${status.network} reaching the internet` : ' · all local')
    : 'Scheduler stopped.';

  const host = $('watchList');
  host.innerHTML = '';
  if (!list.length) {
    host.appendChild(el('p', 'dim',
      'No watches yet. Add one to have it check your mail, your deployments ' +
      'or any URL on a schedule.'));
    return;
  }

  for (const w of list) {
    const overall = w.last_error ? 'failed' : worstState(w.snapshot);
    const card = el('div', `watch-card s-${overall}${w.enabled ? '' : ' off'}`);

    const top = el('div', 'watch-top');
    top.appendChild(el('span', `dot ${overall}`));
    top.appendChild(el('strong', null, w.label));
    top.appendChild(el('span', `reach ${w.local ? 'local' : 'network'}`,
      w.local ? 'local' : 'network'));
    card.appendChild(top);

    if (w.last_error) {
      card.appendChild(el('p', 'dim', w.last_error));
    } else if (!w.snapshot.length) {
      card.appendChild(el('p', 'dim',
        w.last_run ? 'Nothing to report.' : 'Not checked yet.'));
    } else {
      const items = el('div', 'watch-items');
      for (const item of w.snapshot.slice(0, 4)) {
        const row = el('div', 'watch-item');
        row.appendChild(el('span', `dot ${item.state}`));
        row.appendChild(el('b', null, item.title));
        if (item.detail) row.appendChild(el('em', null, item.detail));
        items.appendChild(row);
      }
      if (w.snapshot.length > 4) {
        items.appendChild(el('div', 'watch-item',
          `and ${w.snapshot.length - 4} more`));
      }
      card.appendChild(items);
    }

    const meta = el('div', 'watch-meta');
    meta.appendChild(el('span', null, `every ${humanInterval(w.interval_s)}`));
    meta.appendChild(el('span', null,
      w.last_run ? `checked ${relative(w.last_run)}` : 'never checked'));
    if (w.unseen) meta.appendChild(el('span', null, `${w.unseen} unread`));
    card.appendChild(meta);

    const actions = el('div', 'inline');
    actions.style.marginTop = '8px';
    const check = el('button', 'btn', 'Check now');
    check.addEventListener('click', () => runWatch(w.id, check));
    actions.appendChild(check);

    const toggle = el('button', 'btn', w.enabled ? 'Pause' : 'Resume');
    toggle.addEventListener('click', async () => {
      await api(`/api/watch/${w.id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: !w.enabled }),
      });
      loadWatch();
    });
    actions.appendChild(toggle);

    const remove = el('button', 'btn btn-quiet', 'Remove');
    remove.addEventListener('click', async () => {
      await api(`/api/watch/${w.id}`, { method: 'DELETE' });
      toast(`Stopped watching ${w.label}`);
      loadWatch();
    });
    actions.appendChild(remove);
    card.appendChild(actions);
    host.appendChild(card);
  }
}

function humanInterval(seconds) {
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

function relative(ts) {
  const delta = Date.now() / 1000 - ts;
  if (delta < 90) return 'just now';
  if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
  return `${Math.round(delta / 86400)} d ago`;
}

function renderEvents(events) {
  $('eventCount').textContent = String(events.length);
  const feed = $('eventFeed');
  feed.innerHTML = '';
  if (!events.length) {
    feed.appendChild(el('p', 'dim',
      'Nothing has changed yet. Events appear here when a watch finds ' +
      'something new — not on every poll.'));
    return;
  }
  for (const ev of events) {
    const row = el('div', `feed-row${ev.seen ? '' : ' unseen'}`);
    row.appendChild(el('span', 'src', ev.watcher_label));
    const body = el('div');
    const head = el('b', null, '');
    head.appendChild(el('span', `dot ${ev.state}`));
    head.appendChild(document.createTextNode(' ' + ev.title));
    body.appendChild(head);
    if (ev.detail) body.appendChild(el('span', 'detail', ev.detail));
    if (ev.url) {
      const link = el('a', 'detail', ev.url);
      link.href = ev.url;
      link.target = '_blank';
      link.rel = 'noreferrer';
      body.appendChild(link);
    }
    row.appendChild(body);
    row.appendChild(el('time', null, relative(ev.created_at)));
    feed.appendChild(row);
  }
}

async function runWatch(id, button) {
  const original = button?.textContent;
  if (button) { button.disabled = true; button.textContent = 'Checking…'; }
  try {
    const result = await api(`/api/watch/${id}/run`, { method: 'POST' });
    if (!result.ok) toast(result.error, 'err');
    else if (result.changed) toast(`${result.changed} new`);
    loadWatch();
  } catch (e) { toast(e.message, 'err'); }
  finally { if (button) { button.disabled = false; button.textContent = original; } }
}

async function runAllWatches() {
  const { watchers } = await api('/api/watch');
  if (!watchers.length) { toast('No watches to check'); return; }
  toast(`Checking ${watchers.length}…`);
  await Promise.all(watchers.map((w) =>
    api(`/api/watch/${w.id}/run`, { method: 'POST' }).catch(() => null)));
  loadWatch();
}

async function newWatch(preselect) {
  if (!state.watchCatalog) {
    try { state.watchCatalog = await api('/api/watch/catalog'); }
    catch (e) { toast(e.message, 'err'); return; }
  }
  const all = [
    ...state.watchCatalog.local.map((k) => ({ ...k, local: true })),
    ...state.watchCatalog.services.map((k) => ({ ...k, local: false })),
  ];

  showModal('Add a watch', (body) => {
    const pick = el('div', 'form-field');
    pick.appendChild(el('label', null, 'What should it watch?'));
    const select = el('select');
    select.id = 'watchKind';
    for (const kind of all) {
      const option = el('option', null,
        `${kind.label} — ${kind.local ? 'on this Mac' : 'over the internet'}`);
      option.value = kind.id;
      if (preselect && kind.id === preselect) option.selected = true;
      select.appendChild(option);
    }
    pick.appendChild(select);
    body.appendChild(pick);

    const settings = el('div');
    settings.id = 'watchSettings';
    body.appendChild(settings);

    const row = el('div', 'form-row');
    const every = el('div', 'form-field');
    every.appendChild(el('label', null, 'Check every'));
    const interval = el('select');
    interval.id = 'watchInterval';
    for (const [seconds, label] of [[60, '1 minute'], [300, '5 minutes'],
                                    [900, '15 minutes'], [1800, '30 minutes'],
                                    [3600, '1 hour'], [21600, '6 hours']]) {
      const option = el('option', null, label);
      option.value = String(seconds);
      if (seconds === 300) option.selected = true;
      interval.appendChild(option);
    }
    every.appendChild(interval);
    row.appendChild(every);

    const tellMe = el('div', 'form-field');
    tellMe.appendChild(el('label', null, 'Notify me'));
    const notify = el('select');
    notify.id = 'watchNotify';
    for (const [value, label] of [['problems', 'When something needs me'],
                                  ['all', 'On every change'],
                                  ['off', 'Never — just collect it']]) {
      const option = el('option', null, label);
      option.value = value;
      notify.appendChild(option);
    }
    tellMe.appendChild(notify);
    row.appendChild(tellMe);
    body.appendChild(row);

    const describe = () => {
      const kind = all.find((k) => k.id === select.value);
      settings.innerHTML = '';
      if (!kind) return;
      if (!kind.local && !state.egress?.enabled) {
        const warn = el('p', 'dim',
          'Outbound access is off, so this watch will not run until you turn ' +
          'it on in Connect.');
        warn.style.color = 'var(--warn)';
        settings.appendChild(warn);
      }
      if (!kind.local && kind.needs_token && !kind.token_set) {
        const warn = el('p', 'dim',
          `No ${kind.label} token yet — add one in Connect first.`);
        warn.style.color = 'var(--warn)';
        settings.appendChild(warn);
      }
      for (const [key, hint] of Object.entries(kind.fields || {})) {
        const field = el('div', 'form-field');
        field.appendChild(el('label', null, key.replace(/_/g, ' ')));
        const input = el('input');
        input.dataset.key = key;
        input.placeholder = hint;
        field.appendChild(input);
        settings.appendChild(field);
      }
    };
    select.addEventListener('change', describe);
    describe();
  }, [
    { label: 'Cancel', cls: 'btn-quiet', onClick: (close) => close() },
    {
      label: 'Start watching', cls: 'btn-accent', onClick: async (close) => {
        const kindId = $('watchKind').value;
        const kind = all.find((k) => k.id === kindId);
        const settings = {};
        for (const input of $('watchSettings').querySelectorAll('input[data-key]')) {
          if (input.value.trim()) settings[input.dataset.key] = input.value.trim();
        }
        try {
          await api('/api/watch', {
            method: 'POST',
            body: JSON.stringify({
              kind: kindId,
              label: kind.label,
              settings,
              interval_s: Number($('watchInterval').value),
              notify: $('watchNotify').value,
            }),
          });
          close();
          toast(`Watching ${kind.label}`);
          switchView('watch');
          loadWatch();
        } catch (e) { toast(e.message, 'err'); }
      },
    },
  ]);
}

function startWatchPolling() {
  stopWatchPolling();
  state.watchTimer = setInterval(loadWatch, 20000);
}
function stopWatchPolling() {
  if (state.watchTimer) clearInterval(state.watchTimer);
  state.watchTimer = null;
}

/* The unread count is worth knowing from anywhere, so it refreshes on a slow
   timer even when the Watch view is closed. One small request a minute. */
async function pollWatchBadge() {
  try {
    const { unseen } = await api('/api/watch/events?limit=1');
    updateWatchBadge(unseen);
  } catch { /* the scheduler may simply have nothing yet */ }
}

/* ═══════════════════════════════════════════════════════════════════════
   BUILD — write code, run it, fix it
   ═══════════════════════════════════════════════════════════════════════ */

function loadCodeView() {
  const note = $('codeAutonomyNote');
  const level = state.autonomy;
  note.textContent = level === 'full'
    ? 'Autonomy is full: it will edit files and run tests without asking.'
    : level === 'safe'
      ? 'Autonomy is safe: it can read and search freely, but every edit and ' +
        'test run waits for your approval in Automate.'
      : 'Autonomy is off: every single tool call waits for approval. Raise it ' +
        'in Automate to let a build actually run.';
  note.style.color = level === 'off' ? 'var(--warn)' : 'var(--dim)';
}

async function scanProject() {
  const path = $('codePath').value.trim();
  if (!path) { toast('Give it a project folder', 'err'); return; }
  const host = $('codeSurvey');
  host.innerHTML = '<div class="skeleton"><i></i></div>';
  try {
    const survey = await api('/api/code/scan', {
      method: 'POST', body: JSON.stringify({ path }),
    });
    state.codeSurvey = survey;
    host.innerHTML = '';

    const languages = Object.entries(survey.languages)
      .map(([name, n]) => `${name} ${n}`).join(' · ') || 'nothing recognisable';
    const rows = [
      ['Root', survey.root],
      ['Languages', languages],
      ['Files', `${survey.file_count}${survey.truncated ? '+' : ''}`],
      ['Entry points', survey.entry_points.join(', ') || 'none found'],
      ['Verification', survey.verify_command || 'none detected — set one below'],
    ];
    for (const [label, value] of rows) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'row-label', label));
      row.appendChild(el('span', null, value));
      host.appendChild(row);
    }
    if (survey.verify_command) $('codeVerify').placeholder = survey.verify_command;
    if (!survey.verify_command) {
      const warn = el('p', 'dim',
        'Nothing here declares a test command, so there is no way to prove a ' +
        'change works. Give one below, or it will make the edit and say so ' +
        'plainly rather than claiming success.');
      warn.style.color = 'var(--warn)';
      host.appendChild(warn);
    }
  } catch (e) {
    host.innerHTML = '';
    host.appendChild(el('p', 'dim', e.message));
  }
}

function buildRow(kind, tag, text, pre) {
  const row = el('div', `ev ev-${kind}`);
  row.appendChild(el('span', 'ev-tag', tag));
  const body = el('div', 'ev-body');
  if (text) body.appendChild(el('div', null, text));
  if (pre) body.appendChild(el('pre', null,
    typeof pre === 'string' ? pre : JSON.stringify(pre, null, 1)));
  row.appendChild(body);
  $('codeStream').appendChild(row);
  row.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'nearest' });
  return row;
}

async function runBuild() {
  if (state.buildRun) return;
  const path = $('codePath').value.trim();
  const task = $('codeTask').value.trim();
  if (!path) { toast('Give it a project folder', 'err'); return; }
  if (!task) { toast('Describe the change first', 'err'); return; }

  const stream = $('codeStream');
  stream.innerHTML = '';
  state.buildRun = true;
  state.buildAbort = new AbortController();
  $('codeRun').disabled = true;
  $('codeStop').hidden = false;

  try {
    const res = await fetch('/api/code/build', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: state.buildAbort.signal,
      body: JSON.stringify({
        task, path,
        autonomy: state.autonomy,
        rounds: Number($('codeRounds').value),
        verify_command: $('codeVerify').value.trim(),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Request failed (${res.status})`);
    }

    await readSSE(res, (ev) => {
      if (ev.type === 'build_start') {
        buildRow('run', 'project', `${ev.root} · ${ev.files} files`);
        buildRow('run', 'verify', ev.verify || 'no verification command');
      } else if (ev.type === 'round') {
        const mark = el('div', 'round-mark', `attempt ${ev.n} of ${ev.of}`);
        stream.appendChild(mark);
      } else if (ev.type === 'thinking') {
        buildRow('think', 'step', `thinking (${ev.step})`);
      } else if (ev.type === 'note') {
        buildRow('note', 'plan', ev.text);
      } else if (ev.type === 'tool_call') {
        buildRow('call', ev.tool,
          ev.needs_approval ? 'waiting for your approval in Automate' : '',
          ev.args);
      } else if (ev.type === 'tool_result') {
        buildRow('ok', ev.tool, '', ev.result);
      } else if (ev.type === 'tool_error') {
        buildRow('err', ev.tool, ev.detail);
      } else if (ev.type === 'verifying') {
        buildRow('run', 'running', ev.command);
      } else if (ev.type === 'verified') {
        const verdict = el('div', `verdict ${ev.passed ? 'pass' : 'fail'}`);
        verdict.appendChild(el('strong', null,
          ev.passed ? 'Tests pass' : `Tests failed (exit ${ev.exit_code})`));
        stream.appendChild(verdict);
        if (!ev.passed) buildRow('err', 'output', '', ev.output);
      } else if (ev.type === 'final') {
        buildRow('final', 'said', ev.text);
      } else if (ev.type === 'build_done') {
        const verdict = el('div', `verdict ${ev.verified ? 'pass' : 'fail'}`);
        verdict.appendChild(el('strong', null, ev.verified
          ? `Done and verified in ${ev.rounds} attempt${ev.rounds === 1 ? '' : 's'}`
          : 'Finished without a passing verification'));
        stream.appendChild(verdict);
        if (ev.note) buildRow('note', 'note', ev.note);
        toast(ev.verified ? 'Build verified' : 'Build finished unverified',
              ev.verified ? '' : 'err');
      } else if (ev.type === 'error') {
        buildRow('err', 'error', ev.detail);
      } else if (ev.type === 'cancelled') {
        buildRow('err', 'stopped', 'Cancelled.');
      }
    });
  } catch (e) {
    if (e.name !== 'AbortError') { buildRow('err', 'error', e.message); toast(e.message, 'err'); }
  } finally {
    state.buildRun = false;
    state.buildAbort = null;
    $('codeRun').disabled = false;
    $('codeStop').hidden = true;
  }
}

function stopBuild() {
  state.buildAbort?.abort();
  toast('Build stopped');
}
