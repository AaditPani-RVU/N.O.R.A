// Desktop remote — the focused window as a map you can press.
//
// nora/desktop.py hands over the active window's accessibility tree with
// geometry attached: bounds, plus every showing control at its true rect.
// This file draws that at the window's real aspect ratio and turns a tap
// back into an AT-SPI action. The geometry is the whole point — you are
// looking at a small monochrome copy of the other screen, so you press the
// thing that is in the same place as the thing you can see, and the label
// only has to confirm it.
//
// Deliberately not an orb takeover. A takeover morphs, holds for 45s and
// shrinks back; a remote control that disappears mid-press is a bug. So it
// is its own overlay with an explicit close, and it never auto-hides.
//
// Served same-origin from /remote.js for the same reason takeovers.js and
// geo.js are: index.html is long enough.

(function () {
  'use strict';

  let ctxRef = null;
  let open = false;
  let map = null;          // last rendered payload
  let sig = '';            // signature of what is on screen, see render()
  let busy = false;        // a request is in flight
  let typing = null;       // element id the type bar is aimed at
  let pollId = 0;

  const el = id => document.getElementById(id);
  let root, win, stage, appLbl, titleLbl, countLbl, hint, typeBar, typeIn;

  // How often the map re-reads the window. Renders are suppressed unless the
  // signature actually changed, so a still window costs one request and no
  // DOM work — which is what makes a poll this frequent tolerable while
  // someone is aiming at a 20px target.
  const POLL_MS = 3500;

  function qs(extra) {
    const token = ctxRef && ctxRef.token ? ctxRef.token() : '';
    const parts = [];
    if (token) parts.push('token=' + encodeURIComponent(token));
    if (extra) parts.push(extra);
    return parts.length ? '?' + parts.join('&') : '';
  }

  function say(msg, bad) {
    if (!hint) return;
    hint.textContent = msg;
    hint.classList.toggle('err', !!bad);
  }

  // The dashboard's activity feed is a courtesy, not part of the press. It
  // used to be called bare inside act()'s try, so a throw anywhere in the
  // host page's logging turned a press that had already succeeded into
  // "FAILED — <TypeError>" on screen. Never again.
  function note(msg, ok) {
    try { if (ctxRef && ctxRef.log) ctxRef.log(msg, ok); } catch (_) {}
  }

  // ── Load ───────────────────────────────────────────────────────────
  //
  // `follow` asks the backend to re-map the window this view is already
  // showing rather than whatever has focus. Without it, the moment you touch
  // the dashboard on the same machine the browser becomes the focused
  // window and the remote spends the rest of the session mapping itself.

  async function load(follow) {
    if (busy) return;
    busy = true;
    try {
      const r = await fetch('/desktop' + qs(follow ? 'follow=1' : ''), { cache: 'no-store' });
      if (r.status === 401) { say('UNAUTHORISED — OPEN THE DASHBOARD WITH ?token=…', true); return; }
      const d = await r.json();
      if (!d.ok) { renderEmpty(d.error || 'nothing to map'); return; }
      map = d;
      render(d);
    } catch (e) {
      say('LOST THE WINDOW — ' + e, true);
    } finally {
      busy = false;
    }
  }

  function renderEmpty(msg) {
    map = null; sig = '';
    win.style.width = win.style.height = '';
    win.innerHTML = '<div class="rm-empty">' + esc(String(msg).toUpperCase()) + '</div>';
    countLbl.textContent = '—';
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  // ── Render ─────────────────────────────────────────────────────────

  function render(d) {
    appLbl.textContent = (d.app || 'DESKTOP').toUpperCase();
    titleLbl.textContent = d.title || '';
    countLbl.textContent = d.elements.length + (d.truncated ? '+ CONTROLS' : ' CONTROLS');

    // Re-rendering identical geometry while someone is aiming at a control
    // yanks it out from under them. So a poll that finds nothing changed
    // does nothing at all.
    const next = d.title + '|' + d.elements.map(e =>
      e.id + ',' + e.x + ',' + e.y + ',' + e.w + ',' + e.h + ',' + e.name +
      ',' + e.checked + ',' + e.focused + ',' + e.enabled + ',' + e.value).join(';');
    if (next === sig) return;
    sig = next;

    // Some toolkits register a full tree but never fill in extents, so every
    // control claims the window origin (GTK4 does this today). The backend
    // flags that rather than letting us draw a hundred targets in one corner
    // — laid out in tree order, the remote still works, it just stops being
    // a map.
    const flow = d.positional === false;
    win.dataset.flow = flow ? '1' : '';
    if (flow) { win.style.width = ''; win.style.height = ''; }
    else fitWindow(d.bounds);

    const bw = d.bounds.w || 1, bh = d.bounds.h || 1;
    const html = d.elements.map(e => {
      const label = e.kind === 'input' ? (e.value || e.name || '') : (e.name || e.desc || '');
      const place = flow ? '' :
        ' style="left:' + pct(e.x / bw) + ';top:' + pct(e.y / bh) +
        ';width:' + pct(e.w / bw) + ';height:' + pct(e.h / bh) + '"';
      return '<div class="rm-el" data-id="' + e.id + '"' +
        ' data-kind="' + esc(e.kind) + '"' +
        ' data-enabled="' + (e.enabled ? 1 : 0) + '"' +
        ' data-focused="' + (e.focused ? 1 : 0) + '"' +
        ' data-checked="' + (e.checked ? 1 : 0) + '"' +
        ' title="' + esc((e.name || e.desc || e.role) + ' · ' + e.role) + '"' +
        place + '><span>' + esc(label || e.role) + '</span></div>';
    }).join('');
    win.innerHTML = html || '<div class="rm-empty">NOTHING PRESSABLE IN THIS WINDOW</div>';
    if (flow) say('NO LAYOUT FROM THIS APP — LISTED IN TREE ORDER');
  }

  function pct(v) { return (v * 100).toFixed(3) + '%'; }

  // The window keeps its true aspect ratio at the largest size the stage can
  // hold. Everything inside is positioned in percentages, so this is the only
  // place that has to know about pixels.
  function fitWindow(b) {
    const bw = b.w || 1, bh = b.h || 1;
    const box = stage.getBoundingClientRect();
    const avail = { w: Math.max(120, box.width - 4), h: Math.max(90, box.height - 4) };
    const scale = Math.min(avail.w / bw, avail.h / bh);
    win.style.width = Math.round(bw * scale) + 'px';
    win.style.height = Math.round(bh * scale) + 'px';
  }

  // ── Act ────────────────────────────────────────────────────────────

  async function act(node, action, text) {
    if (!map) return;
    const id = parseInt(node.dataset.id, 10);
    node.classList.remove('fail');
    node.classList.add('hit');
    say('…');
    try {
      const r = await fetch('/desktop_act' + qs(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ snap: map.snap, id, action, text: text || '' }),
      });
      if (r.status === 401) { say('UNAUTHORISED', true); node.classList.add('fail'); return; }
      const d = await r.json();
      if (d.ok) {
        const label = d.label || 'control';
        say((action === 'type' ? 'TYPED INTO ' : 'PRESSED ') + label.toUpperCase());
        note((action === 'type' ? 'Typed into ' : 'Pressed ') + label, true);
      } else {
        node.classList.add('fail');
        say((d.error || 'failed').toUpperCase(), true);
        note('Remote: ' + (d.error || 'failed'), false);
      }
      // Either way the window may have moved on — a stale snapshot is the
      // one error worth recovering from without being asked.
      setTimeout(() => load(true), d.stale ? 0 : 420);
    } catch (e) {
      node.classList.add('fail');
      say('FAILED — ' + e, true);
    } finally {
      setTimeout(() => node.classList.remove('hit'), 260);
    }
  }

  function onTap(ev) {
    const node = ev.target.closest('.rm-el');
    if (!node || !win.contains(node)) return;
    const kind = node.dataset.kind;
    if (kind === 'input' || kind === 'combo') {
      openTypeBar(node);
      return;
    }
    closeTypeBar();
    act(node, 'click');
  }

  // ── Type bar ───────────────────────────────────────────────────────
  // A text field is the case voice handles worst and a phone handles best:
  // tapping one here brings up the device's own keyboard.

  function openTypeBar(node) {
    typing = node.dataset.id;
    typeBar.hidden = false;
    typeIn.value = '';
    typeIn.placeholder = (node.title.split(' · ')[0] || 'TEXT').toUpperCase();
    typeIn.focus();
    say('TYPE, THEN SEND');
  }

  function closeTypeBar() {
    typing = null;
    typeBar.hidden = true;
    typeIn.blur();
  }

  function submitType(ev) {
    ev.preventDefault();
    if (typing == null) return;
    const node = win.querySelector('.rm-el[data-id="' + typing + '"]');
    const text = typeIn.value;
    closeTypeBar();
    if (node) act(node, 'type', text);
  }

  // ── Lifecycle ──────────────────────────────────────────────────────

  function show() {
    if (open) { load(true); return; }
    open = true;
    root.hidden = false;
    // One frame between unhiding and the class so the transition actually
    // runs rather than being collapsed into the same style recalculation.
    requestAnimationFrame(() => root.classList.add('in'));
    say('TAP A CONTROL TO PRESS IT');
    load(false);
    clearInterval(pollId);
    pollId = setInterval(() => {
      // Never poll over someone mid-sentence, and never poll a tab nobody
      // is looking at.
      if (!open || typing != null || document.hidden) return;
      load(true);
    }, POLL_MS);
  }

  function hide() {
    if (!open) return;
    open = false;
    clearInterval(pollId); pollId = 0;
    closeTypeBar();
    root.classList.remove('in');
    setTimeout(() => { if (!open) root.hidden = true; }, 260);
  }

  window.NORA_REMOTE = {
    init(context) {
      ctxRef = context;
      root = el('remote'); win = el('rm-window'); stage = el('rm-stage');
      appLbl = el('rm-app'); titleLbl = el('rm-title'); countLbl = el('rm-count');
      hint = el('rm-hint'); typeBar = el('rm-type'); typeIn = el('rm-input');
      if (!root) return;

      win.addEventListener('click', onTap);
      typeBar.addEventListener('submit', submitType);
      el('rm-close').addEventListener('click', hide);
      el('rm-refresh').addEventListener('click', () => { sig = ''; load(true); });
      root.addEventListener('click', ev => { if (ev.target === root) hide(); });
      addEventListener('resize', () => { if (open && map) fitWindow(map.bounds); });
      addEventListener('keydown', ev => {
        if (!open) return;
        if (ev.key === 'Escape') { if (typing != null) closeTypeBar(); else hide(); }
      }, true);
    },
    show, hide,
    active: () => open,
  };
})();
