/* Drawing-RNG Stroke Token Encoder — cleaner collection UI v0.4 */

const $ = (id) => document.getElementById(id);

// ── Element refs ────────────────────────────────────────────────────────────
const canvas           = $('drawCanvas');
const ctx              = canvas.getContext('2d');
const canvasWrap       = $('canvasWrap');
const strokeCountEl    = $('strokeCount');

const profileEl        = $('profileSelect');
const orderModeEl      = $('orderMode');
const spacingEl        = $('spacing');
const spacingLabelEl   = $('spacingLabel');
const dirBucketsEl     = $('directionBuckets');
const lenShortMaxEl    = $('lenShortMax');
const lenShortMaxLabel = $('lenShortMaxLabel');
const lenMedMaxEl      = $('lenMedMax');
const lenMedMaxLabel   = $('lenMedMaxLabel');
const zoneGridEl       = $('zoneGrid');
const minStrokeLenEl   = $('minStrokeLength');
const minStrokeLenLabel= $('minStrokeLengthLabel');
const simplifyEl       = $('simplifyEpsilon');
const simplifyLabelEl  = $('simplifyEpsilonLabel');
const jitterEl         = $('jitterRunMax');
const turnTokensEl     = $('includeTurnTokens');
const turnMagnitudeEl  = $('includeTurnMagnitude');
const startZoneEl      = $('includeStartZone');
const penupMovesEl     = $('includePenupMoves');
const closedTokensEl   = $('includeClosedTokens');
const relationTokensEl = $('includeRelationTokens');

const promptSelectEl   = $('promptSelect');
const activePromptEl   = $('activePromptText');
const redrawIndexEl    = $('redrawIndex');
const sampleNameEl     = $('sampleName');
const autoNameEl       = $('autoNamePreview');
const notesEl          = $('notes');
const consentEl        = $('consentCheckbox');
const participantView  = $('participantIdView');
const saveStatusEl     = $('saveStatus');
const errorBarEl       = $('errorBar');

const serializedEl     = $('serializedOutput');
const statsEl          = $('statsOutput');
const tokensEl         = $('tokensOutput');
const streamEl         = $('streamOutput');
const similarityEl     = $('similarityOutput');

let strokes       = [];
let currentStroke = null;
let lastTokens    = null;
let latestSerialized = '';

// ── Anonymous participant id ───────────────────────────────────────────────
function randomId() {
  const bytes = new Uint8Array(4);
  crypto.getRandomValues(bytes);
  return 'p_' + Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}
const participantId = localStorage.getItem('drng_participant_id') || randomId();
localStorage.setItem('drng_participant_id', participantId);
participantView.textContent = participantId;

// ── Canvas helpers ─────────────────────────────────────────────────────────
function getPoint(e) {
  const rect = canvas.getBoundingClientRect();
  return [
    (e.clientX - rect.left) * (canvas.width  / rect.width),
    (e.clientY - rect.top)  * (canvas.height / rect.height),
  ];
}

function drawBackground() {
  ctx.fillStyle = '#fbfdff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = 'rgba(15,23,42,0.06)';
  ctx.lineWidth = 2;
  for (let x = 75; x < canvas.width; x += 75) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke();
  }
  for (let y = 75; y < canvas.height; y += 75) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke();
  }
}

function drawStroke(stroke, color = '#152033', width = 6) {
  if (!stroke || stroke.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth   = width;
  ctx.lineCap     = 'round';
  ctx.lineJoin    = 'round';
  ctx.beginPath();
  ctx.moveTo(stroke[0][0], stroke[0][1]);
  for (let i = 1; i < stroke.length; i++) ctx.lineTo(stroke[i][0], stroke[i][1]);
  ctx.stroke();
}

function redraw() {
  drawBackground();
  strokes.forEach(s => drawStroke(s));
  if (currentStroke) drawStroke(currentStroke, '#0284c7', 6);
  const n = strokes.length;
  strokeCountEl.textContent = `${n} stroke${n === 1 ? '' : 's'}`;
}

// ── Pointer events ─────────────────────────────────────────────────────────
canvas.addEventListener('pointerdown', e => {
  e.preventDefault();
  canvas.setPointerCapture(e.pointerId);
  currentStroke = [getPoint(e)];
  canvasWrap.classList.add('drawing');
  redraw();
});

canvas.addEventListener('pointermove', e => {
  if (!currentStroke) return;
  e.preventDefault();
  const p = getPoint(e);
  const last = currentStroke[currentStroke.length - 1];
  if (Math.hypot(p[0] - last[0], p[1] - last[1]) >= 1.5) {
    currentStroke.push(p);
    redraw();
  }
});

['pointerup', 'pointercancel'].forEach(ev =>
  canvas.addEventListener(ev, e => {
    if (!currentStroke) return;
    e.preventDefault();
    if (currentStroke.length >= 2) strokes.push(currentStroke);
    currentStroke = null;
    canvasWrap.classList.remove('drawing');
    redraw();
  })
);

canvas.addEventListener('pointerleave', e => {
  if (currentStroke) canvas.dispatchEvent(new PointerEvent('pointerup', e));
});

// ── Naming / prompts ────────────────────────────────────────────────────────
function pad2(n) { return String(n).padStart(2, '0'); }
function selectedPromptLabel() {
  return promptSelectEl.options[promptSelectEl.selectedIndex]?.text || promptSelectEl.value;
}
function autoName() {
  return `${promptSelectEl.value}_redraw_${pad2(parseInt(redrawIndexEl.value || '1', 10))}`;
}
function syncSampleName() {
  activePromptEl.textContent = selectedPromptLabel();
  autoNameEl.textContent = sampleNameEl.value.trim() || autoName();
}

// ── Params ─────────────────────────────────────────────────────────────────
function getParams() {
  return {
    resample_spacing:             parseFloat(spacingEl.value),
    direction_buckets:            parseInt(dirBucketsEl.value, 10),
    length_buckets: {
      short_max:  parseFloat(lenShortMaxEl.value),
      medium_max: parseFloat(lenMedMaxEl.value),
    },
    zone_grid:                    parseInt(zoneGridEl.value, 10),
    order_mode:                   orderModeEl.value,
    min_stroke_points:            2,
    min_raw_stroke_length:        5.0,
    min_normalized_stroke_length: parseFloat(minStrokeLenEl.value),
    jitter_run_max:               parseInt(jitterEl.value, 10),
    simplify_epsilon:             parseFloat(simplifyEl.value),
    include_turn_tokens:          turnTokensEl.checked,
    include_turn_magnitude:       turnMagnitudeEl.checked,
    include_start_zone:           startZoneEl.checked,
    include_penup_moves:          penupMovesEl.checked,
    include_closed_tokens:        closedTokensEl.checked,
    include_relation_tokens:      relationTokensEl.checked,
    close_threshold:              0.075,
    round_normalized:             4,
  };
}

// ── Similarity helpers ─────────────────────────────────────────────────────
function editDistance(a, b) {
  const prev = [...Array(b.length + 1).keys()];
  for (let i = 1; i <= a.length; i++) {
    const curr = [i];
    for (let j = 1; j <= b.length; j++) {
      curr.push(Math.min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (a[i-1] === b[j-1] ? 0 : 1)));
    }
    prev.splice(0, prev.length, ...curr);
  }
  return prev[b.length];
}
function tokenSimilarity(a, b) {
  const denom = Math.max(a.length, b.length, 1);
  return Math.max(0, 1 - editDistance(a, b) / denom);
}
function tokenKind(t) {
  if (t === 'END') return 'end';
  if (t === 'CLOSED') return 'structure';
  if (t === 'S' || t.startsWith('S@')) return 'structure';
  if (t === 'PU' || t.startsWith('PU_')) return 'penup';
  if (t.startsWith('REL_')) return 'relation';
  if (['TR','TL','TU','TS'].includes(t) || t.startsWith('TR_') || t.startsWith('TL_')) return 'turn';
  return 'direction';
}
function filterTokens(tokens, kind) { return tokens.filter(t => tokenKind(t) === kind); }
function similarityReport(prev, now) {
  const overall   = tokenSimilarity(prev, now);
  const direction = tokenSimilarity(filterTokens(prev, 'direction'), filterTokens(now, 'direction'));
  const structure = tokenSimilarity(filterTokens(prev, 'structure'), filterTokens(now, 'structure'));
  const relation  = tokenSimilarity(filterTokens(prev, 'relation'),  filterTokens(now, 'relation'));
  const turn      = tokenSimilarity(filterTokens(prev, 'turn'),      filterTokens(now, 'turn'));
  const penup     = tokenSimilarity(filterTokens(prev, 'penup'),     filterTokens(now, 'penup'));
  return [
    `${overall.toFixed(3)} overall similarity`,
    `direction ${direction.toFixed(3)} · structure ${structure.toFixed(3)} · pen-up ${penup.toFixed(3)}`,
    `relation ${relation.toFixed(3)} · turn ${turn.toFixed(3)}`,
    `prev ${prev.length} tokens → now ${now.length} tokens`,
  ].join('\n');
}

// ── Error helpers ──────────────────────────────────────────────────────────
function showError(msg) { errorBarEl.textContent = msg; errorBarEl.classList.add('visible'); }
function clearError() { errorBarEl.textContent = ''; errorBarEl.classList.remove('visible'); }

// ── Tokenize ───────────────────────────────────────────────────────────────
async function tokenize() {
  clearError();
  if (strokes.length === 0) { showError('Draw at least one stroke first.'); return; }
  const btn = $('tokenizeBtn');
  btn.textContent = 'Encoding…'; btn.disabled = true;
  try {
    const res  = await fetch('/api/tokenize', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ strokes, params: getParams() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Tokenization failed');

    latestSerialized         = data.serialized;
    serializedEl.textContent = data.serialized;
    tokensEl.textContent     = JSON.stringify(data.tokens, null, 2);
    streamEl.textContent     = data.seed_material_hex || '—';

    const flags = data.stats?.weak_seed_flags || [];
    statsEl.textContent = flags.length ? '⚠ ' + flags.join('\n⚠ ') : '✓ No weak-seed flags';

    similarityEl.textContent = lastTokens ? similarityReport(lastTokens, data.tokens) : 'Generate a second drawing to compare.';
    lastTokens = data.tokens;
  } catch (err) {
    showError(err.message);
  } finally {
    btn.textContent = 'Generate tokens'; btn.disabled = false;
  }
}

// ── Server-side save ───────────────────────────────────────────────────────
async function saveSample() {
  if (strokes.length === 0) {
    saveStatusEl.className = 'status-line bad'; saveStatusEl.textContent = 'Draw something first.'; return;
  }
  if (!consentEl.checked) {
    saveStatusEl.className = 'status-line bad'; saveStatusEl.textContent = 'Please confirm the anonymous research data notice first.'; return;
  }
  const btn = $('downloadBtn');
  btn.textContent = 'Saving…'; btn.disabled = true;
  saveStatusEl.className = 'status-line'; saveStatusEl.textContent = '';

  const sampleName = sampleNameEl.value.trim() || autoName();
  try {
    const res = await fetch('/api/save_sample', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
  strokes,
  params: getParams(),
  participant_id: participantId,
  concept: conceptEl.value,
  redraw_id: parseInt(redrawEl.value, 10),
  name: sampleNameEl.value || 'sample',
  notes: notesEl.value || '',
  canvas_size: [canvas.width, canvas.height],
  serialized: latestSerialized || null,
  ui_version: 'clean-ui-v1'
}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Save failed');
    saveStatusEl.className = 'status-line ok';
    saveStatusEl.textContent = `✓ Saved: ${data.filename}`;
  } catch (err) {
    saveStatusEl.className = 'status-line bad';
    saveStatusEl.textContent = `✗ ${err.message}`;
  } finally {
    btn.textContent = 'Save drawing'; btn.disabled = false;
  }
}

// ── Profiles ───────────────────────────────────────────────────────────────
const PROFILES = {
  tolerant: {
    orderMode: 'spatial', spacing: '0.08', directionBuckets: '4', lenShortMax: '0.25', lenMedMax: '0.60', zoneGrid: '2', minStrokeLength: '0.050', simplifyEpsilon: '0.030', jitterRunMax: '2', turnTokens: true, turnMagnitude: false, startZone: true, penupMoves: true, closedTokens: true, relationTokens: true,
  },
  balanced: {
    orderMode: 'spatial', spacing: '0.05', directionBuckets: '8', lenShortMax: '0.18', lenMedMax: '0.40', zoneGrid: '3', minStrokeLength: '0.035', simplifyEpsilon: '0.015', jitterRunMax: '1', turnTokens: true, turnMagnitude: false, startZone: true, penupMoves: true, closedTokens: true, relationTokens: true,
  },
  strict: {
    orderMode: 'drawn', spacing: '0.035', directionBuckets: '16', lenShortMax: '0.12', lenMedMax: '0.28', zoneGrid: '4', minStrokeLength: '0.020', simplifyEpsilon: '0.005', jitterRunMax: '0', turnTokens: true, turnMagnitude: true, startZone: true, penupMoves: true, closedTokens: true, relationTokens: true,
  },
};
function syncLabels() {
  spacingLabelEl.textContent    = spacingEl.value;
  minStrokeLenLabel.textContent = minStrokeLenEl.value;
  lenShortMaxLabel.textContent  = lenShortMaxEl.value;
  lenMedMaxLabel.textContent    = lenMedMaxEl.value;
  simplifyLabelEl.textContent   = simplifyEl.value;
}
function applyProfile(name) {
  if (!PROFILES[name]) return;
  const p = PROFILES[name];
  orderModeEl.value = p.orderMode; spacingEl.value = p.spacing; dirBucketsEl.value = p.directionBuckets;
  lenShortMaxEl.value = p.lenShortMax; lenMedMaxEl.value = p.lenMedMax; zoneGridEl.value = p.zoneGrid;
  minStrokeLenEl.value = p.minStrokeLength; simplifyEl.value = p.simplifyEpsilon; jitterEl.value = p.jitterRunMax;
  turnTokensEl.checked = p.turnTokens; turnMagnitudeEl.checked = p.turnMagnitude; startZoneEl.checked = p.startZone;
  penupMovesEl.checked = p.penupMoves; closedTokensEl.checked = p.closedTokens; relationTokensEl.checked = p.relationTokens;
  syncLabels();
}
function markCustom() { if (profileEl.value !== 'custom') profileEl.value = 'custom'; }

// ── Wire up ────────────────────────────────────────────────────────────────
$('tokenizeBtn').addEventListener('click', tokenize);
$('downloadBtn').addEventListener('click', saveSample);
$('undoBtn').addEventListener('click', () => { strokes.pop(); redraw(); });
$('clearBtn').addEventListener('click', () => {
  strokes = []; currentStroke = null; latestSerialized = ''; lastTokens = null;
  redraw(); clearError(); saveStatusEl.textContent = '';
});
$('copySerialBtn').addEventListener('click', async () => {
  if (!latestSerialized) return;
  await navigator.clipboard.writeText(latestSerialized);
  const prev = $('copySerialBtn').textContent;
  $('copySerialBtn').textContent = 'Copied ✓';
  setTimeout(() => { $('copySerialBtn').textContent = prev; }, 1400);
});
profileEl.addEventListener('change', () => applyProfile(profileEl.value));
promptSelectEl.addEventListener('change', syncSampleName);
redrawIndexEl.addEventListener('input', syncSampleName);
sampleNameEl.addEventListener('input', syncSampleName);
[spacingEl, minStrokeLenEl, lenShortMaxEl, lenMedMaxEl, simplifyEl].forEach(el => el.addEventListener('input', () => { syncLabels(); markCustom(); }));
[orderModeEl, dirBucketsEl, zoneGridEl, jitterEl, turnTokensEl, turnMagnitudeEl, startZoneEl, penupMovesEl, closedTokensEl, relationTokensEl].forEach(el => el.addEventListener('change', markCustom));

applyProfile('balanced');
syncSampleName();
redraw();
