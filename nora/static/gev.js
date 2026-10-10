// The live globe — God's Eye View in a panel (Sharp Phase H).
//
// When NORA answers a globe question ("what's flying over Delhi", "when does
// the ISS pass over me") the core pushes a takeover of kind "globe" carrying
// the GEV view that shows the answer. This panel frames GEV in embed mode
// (?embed=1: just the globe, no HUD) and posts the view to it as `gev:view`,
// so the globe flies to what NORA just said. Later answers reuse the same
// frame; it only moves.
//
// Like the desktop remote it is an overlay with an explicit close, not an
// orb morph: a globe you are looking around in should not shrink away on a
// timer. The frame is built on the first view and dropped a minute after the
// panel closes, so the WebGL globe only costs anything while it is useful.
//
// Loading the globe takes seconds, so `warm()` starts it, invisibly, the
// moment the user asks something that sounds like a globe question: the load
// overlaps NORA working out the answer instead of following it.
//
// GEV refuses to be framed unless its GEV_EMBED_FRAME_ANCESTORS names this
// page (nora-globe.service sets it for the dashboard's localhost origins).

(function () {
  'use strict';

  const DROP_AFTER_MS = 60000;
  // Before any answer has said where GEV is: the same host, GEV's port.
  const DEFAULT_APP = location.protocol + '//' + (location.hostname || '127.0.0.1') + ':5173';
  let warmTimer = 0;
  let root = null, frame = null, appOrigin = '', ready = false, pending = null, dropTimer = 0, seq = 0;

  function css() {
    const s = document.createElement('style');
    s.textContent = `
      #gev { position:fixed; inset:0; z-index:200; display:flex; align-items:stretch; justify-content:center;
             padding:20px; background:rgba(0,4,9,0.82); backdrop-filter:blur(6px);
             opacity:0; transition:opacity var(--t-mid, .35s) var(--ease, ease); }
      #gev[hidden] { display:none; }
      #gev.in { opacity:1; }
      #gev.warming { opacity:0; pointer-events:none; }
      #gev .gv-frame { position:relative; width:min(1280px,100%); height:100%; display:flex; flex-direction:column;
             gap:10px; padding:14px; background:var(--cbg, #020a12);
             border:1px solid rgba(var(--rgb, 0,212,255),0.30);
             box-shadow:0 0 40px rgba(var(--rgb, 0,212,255),0.06), inset 0 0 24px rgba(var(--rgb, 0,212,255),0.02); }
      #gev .gv-head { display:flex; align-items:center; justify-content:space-between; gap:12px; }
      #gev .gv-title { font:600 11px/1 var(--mono, monospace); letter-spacing:.28em; color:rgba(var(--rgb, 0,212,255),0.9); }
      #gev .gv-meta { font:10px/1 var(--mono, monospace); letter-spacing:.16em; color:rgba(var(--rgb, 0,212,255),0.55); }
      #gev .gv-close { font:600 10px/1 var(--mono, monospace); letter-spacing:.22em; padding:8px 12px; cursor:pointer;
             color:rgba(var(--rgb, 0,212,255),0.9); background:rgba(var(--rgb, 0,212,255),0.06);
             border:1px solid rgba(var(--rgb, 0,212,255),0.35); }
      #gev .gv-stage { position:relative; flex:1; min-height:0; border:1px solid rgba(var(--rgb, 0,212,255),0.14); }
      #gev iframe { position:absolute; inset:0; width:100%; height:100%; border:0; background:#000; }
      #gev .gv-wait { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
             font:10px/1 var(--mono, monospace); letter-spacing:.3em; color:rgba(var(--rgb, 0,212,255),0.5); }
      @media (max-width: 640px) { #gev { padding:8px; } #gev .gv-frame { padding:10px; } }
    `;
    document.head.appendChild(s);
  }

  function build() {
    css();
    root = document.createElement('div');
    root.id = 'gev';
    root.hidden = true;
    root.innerHTML = `
      <div class="gv-frame">
        <div class="gv-head">
          <div class="gv-title">GLOBE</div>
          <div class="gv-meta" id="gv-meta">GOD'S EYE VIEW</div>
          <button class="gv-close" id="gv-close" type="button">CLOSE</button>
        </div>
        <div class="gv-stage" id="gv-stage"><div class="gv-wait" id="gv-wait">LOADING THE GLOBE…</div></div>
      </div>`;
    document.body.appendChild(root);
    root.querySelector('#gv-close').addEventListener('click', hide);
    root.addEventListener('click', (e) => { if (e.target === root) hide(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !root.hidden) hide(); });
    window.addEventListener('message', onMessage);
  }

  function onMessage(e) {
    if (!frame || e.source !== frame.contentWindow || e.origin !== appOrigin) return;
    const d = e.data || {};
    if (d.type === 'gev:ready') {
      ready = true;
      const w = root.querySelector('#gv-wait');
      if (w) w.remove();
      if (pending) post(pending);
    } else if (d.type === 'gev:view-applied' && !d.ok) {
      console.warn('[NORA globe] view not applied', d.steps);
    }
  }

  function post(view) {
    pending = view;
    if (!ready || !frame) return;
    frame.contentWindow.postMessage({ type: 'gev:view', id: 'nora-' + (++seq), view }, appOrigin);
    pending = null;
  }

  function ensureFrame(app) {
    let origin;
    try { origin = new URL(app).origin; } catch (_) { return false; }
    if (frame && origin === appOrigin) return true;
    if (frame) frame.remove();
    appOrigin = origin;
    ready = false;
    frame = document.createElement('iframe');
    frame.title = "God's Eye View";
    frame.allow = 'fullscreen';
    frame.src = origin + '/?embed=1';
    const stage = root.querySelector('#gv-stage');
    if (!stage.querySelector('#gv-wait')) {
      const w = document.createElement('div');
      w.className = 'gv-wait'; w.id = 'gv-wait'; w.textContent = 'LOADING THE GLOBE…';
      stage.appendChild(w);
    }
    stage.insertBefore(frame, stage.firstChild);
    return true;
  }

  function warm(app) {
    if (!root) build();
    if (!root.hidden && !root.classList.contains('warming')) return;   // already showing
    if (!ensureFrame(app || (frame ? appOrigin : DEFAULT_APP))) return;
    clearTimeout(dropTimer);
    // Laid out but invisible: a frame under display:none has no size, and
    // the globe would not start until it was shown.
    root.classList.add('warming');
    root.hidden = false;
    clearTimeout(warmTimer);
    warmTimer = setTimeout(() => {          // no globe answer came: put it away
      if (root.classList.contains('warming')) { root.hidden = true; root.classList.remove('warming'); hide(true); }
    }, 45000);
  }

  function show(payload) {
    if (!root) build();
    const p = payload || {};
    if (!p.view || !p.app || !ensureFrame(p.app)) return;
    clearTimeout(dropTimer);
    clearTimeout(warmTimer);
    root.classList.remove('warming');
    const cam = p.view.camera || {};
    const meta = root.querySelector('#gv-meta');
    if (meta && cam.lat != null) {
      meta.textContent = `${(p.view.layers || []).join(' · ').toUpperCase() || 'VIEW'}  ·  ` +
        `${Number(cam.lat).toFixed(2)}, ${Number(cam.lon).toFixed(2)}`;
    }
    root.hidden = false;
    requestAnimationFrame(() => root.classList.add('in'));
    post(p.view);
  }

  function hide(force) {
    if (!root || (root.hidden && !force)) return;
    root.classList.remove('in');
    setTimeout(() => { if (!root.classList.contains('in')) root.hidden = true; }, 350);
    clearTimeout(dropTimer);
    dropTimer = setTimeout(() => {
      if (frame && root.hidden) { frame.remove(); frame = null; ready = false; }
    }, DROP_AFTER_MS);
  }

  window.NORA_GEV = { show, hide, warm };
})();
