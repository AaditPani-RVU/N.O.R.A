// Orb takeovers — the process and memory constellations.
//
// The globe established the pattern this file generalises: the orb is not a
// status light, it is a display surface. On an intent it morphs into a
// domain instrument, holds, and shrinks back. The globe's own code stays
// exactly where it was in index.html — this file adds siblings rather than
// refactoring a working thing, and reuses its visual language (the same
// measured fit, the same veil, the same --ease timings) so the three read as
// one system.
//
// Two surfaces live here:
//
//   processes  live process table as a phyllotactic constellation; tap a
//              body to see it, kill it from there.
//   memory     ChromaDB embeddings projected onto a sphere by the backend,
//              drawn with the globe's own orthographic drop.
//
// The album cover used to be a third surface — sphere-mapped onto the orb
// while music played. A square sleeve wrapped around a ball is a distortion
// with no upside, so the artwork now lives in the Spotify card where it can
// be shown square and large; index.html owns it.
//
// Served same-origin from /takeovers.js for the same reason geo.js is: it
// keeps index.html readable, not because anything here needs a network.

(function () {
  'use strict';

  const TAU = Math.PI * 2, D2R = Math.PI / 180;

  // Matches the globe's drawing space exactly, so anything projected here
  // lands where the equivalent globe feature would.
  const GW = 340, GCX = 170, GCY = 170, GR = 116;
  const TILT_REST = 23 * D2R;

  // Fixed screen-space key light, upper-left — same choice, same reason as
  // the globe: a drifting terminator leaves half your data in the dark.
  const LX = -0.42, LY = -0.46;

  let ctxRef = null;          // wiring handed in by index.html
  let active = null;          // 'processes' | 'memory' | null
  let rafId = 0;

  // ══════════════════════════════════════════════════════════════════
  //  SHARED
  // ══════════════════════════════════════════════════════════════════

  let _accent = [0, 212, 255], _accentAt = 0;
  function accent() {
    // Re-reading computed style every frame is a layout read on the hot
    // path; the accent only moves on orb-state changes, so 250 ms of
    // staleness is invisible and free.
    const now = performance.now();
    if (now - _accentAt > 250) {
      _accentAt = now;
      const raw = getComputedStyle(document.body).getPropertyValue('--rgb');
      const p = raw.split(',').map(n => parseInt(n, 10));
      if (p.length === 3 && p.every(n => !isNaN(n))) _accent = p;
    }
    return _accent;
  }
  function rgba(a, c) { const k = c || accent(); return `rgba(${k[0]},${k[1]},${k[2]},${a})`; }

  // Ease used for every enter/settle animation here, matching --ease.
  function easeOut(t) { return 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3); }

  function fmtBytes(n) {
    if (!n) return '0';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return (n / Math.pow(1024, i)).toFixed(i >= 2 ? 1 : 0) + u[i];
  }

  function fmtAgo(ts) {
    if (!ts) return '';
    const s = Date.now() / 1000 - ts;
    if (s < 90) return Math.max(1, Math.round(s)) + 's ago';
    if (s < 5400) return Math.round(s / 60) + 'm ago';
    if (s < 172800) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }

  // ── Fit ────────────────────────────────────────────────────────────
  // Lifted from the globe's fitGlobe(): the orb sits in the upper third of
  // the centre column, which is right at orb size and wrong at takeover
  // size. Measure how far to slide down to land in the middle of #main and
  // the largest scale that still fits, rather than hardcoding a number that
  // is wrong on either a laptop or a 1080x2424 phone.
  //
  // Kept on its own custom properties (--tk-fit) rather than the globe's, so
  // the two takeovers can never clobber each other's measurement.
  const TK_EXTENT = GR * 1.30 / GW;

  function fit() {
    if (!ctxRef) return;
    const main = ctxRef.main, wrap = ctxRef.orbWrap;
    if (!main || !wrap) return;
    const box = main.getBoundingClientRect();
    const r = wrap.getBoundingClientRect();
    const applied = active ? parseFloat(wrap.style.getPropertyValue('--tk-fit-y')) || 0 : 0;
    const shift = (box.top + box.height / 2) - (r.top + r.height / 2) + applied;
    const orbScale = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue('--orb-scale')) || 1;

    // Phones are tall and narrow: the binding dimension is width, and the
    // detail card needs room underneath. Reserve it here rather than letting
    // the constellation grow under a sheet that then covers half of it.
    // Only give up room for the sheet while one is actually open — an
    // unconditional reserve shrinks the field for the whole session to make
    // space for a card that is usually not there.
    const narrow = box.width < 560;
    const reserve = (narrow && cardOpen) ? 132 : 0;
    const half = Math.min(box.height - reserve, box.width) / 2 - 10;
    const f = Math.max(1.15, Math.min(4.2, half / (GW * TK_EXTENT * orbScale)));
    wrap.style.setProperty('--tk-fit', f.toFixed(3));
    wrap.style.setProperty('--tk-fit-y', Math.round(shift - reserve / 2) + 'px');
  }

  // ── Detail card ────────────────────────────────────────────────────
  // One card, reused by both constellations. On a wide screen it floats
  // beside the field; under 560px it becomes a bottom sheet, because a
  // floating card on a 360px-wide phone covers the thing it describes.
  let cardOpen = false;
  function card() { return document.getElementById('tk-card'); }

  function showCard(html, actions) {
    const el = card();
    if (!el) return;
    el.innerHTML = html;
    if (actions && actions.length) {
      const row = document.createElement('div');
      row.className = 'tk-card-actions';
      actions.forEach(a => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'tk-btn' + (a.danger ? ' danger' : '');
        b.textContent = a.label;
        b.addEventListener('click', ev => { ev.stopPropagation(); a.run(); });
        row.appendChild(b);
      });
      el.appendChild(row);
    }
    el.hidden = false;
    requestAnimationFrame(() => el.classList.add('in'));
    if (!cardOpen) { cardOpen = true; fit(); }
  }

  function hideCard() {
    const el = card();
    if (!el) return;
    el.classList.remove('in');
    if (cardOpen) { cardOpen = false; fit(); }
    // Matches --t-mid so the node deselect and the card fade finish together.
    setTimeout(() => { if (!el.classList.contains('in')) el.hidden = true; }, 380);
  }

  // ── Label under the field ──────────────────────────────────────────
  function setLabel(text) {
    const el = document.getElementById('tk-label');
    if (el) el.textContent = text || '';
  }

  // ══════════════════════════════════════════════════════════════════
  //  CONSTELLATION BASE
  // ══════════════════════════════════════════════════════════════════

  let tk = {
    canvas: null, ctx: null, px: 2, size: 0,
    rot: 0, targetRot: 0, tilt: TILT_REST,
    drag: null, vel: 0,
    enterAt: 0, selected: null, hit: [],
  };

  function tkInit(canvas) {
    tk.canvas = canvas;
    tk.px = Math.min(4, Math.max(2.2, (devicePixelRatio || 1) * 2.6));
    tk.size = Math.round(GW * tk.px);
    canvas.width = canvas.height = tk.size;
    tk.ctx = canvas.getContext('2d');
    tk.ctx.setTransform(tk.px, 0, 0, tk.px, 0, 0);
    bindPointer(canvas);
  }

  // Pointer events cover mouse, pen and touch with one code path, which is
  // the whole reason to use them here — the constellations have to be
  // draggable and tappable on a phone, and a separate touch path would
  // drift out of sync with the mouse one.
  function bindPointer(canvas) {
    let moved = 0;
    canvas.addEventListener('pointerdown', e => {
      if (!active) return;
      // Capture keeps a drag alive if the finger leaves the canvas. Not
      // every pointer id is capturable (synthetic events, some pen stacks),
      // and a throw here would kill the whole gesture.
      try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
      tk.drag = { x: e.clientX, last: e.clientX, t: performance.now() };
      moved = 0;
      e.preventDefault();
    });
    canvas.addEventListener('pointermove', e => {
      if (!tk.drag) return;
      const dx = e.clientX - tk.drag.last;
      tk.drag.last = e.clientX;
      moved += Math.abs(dx);
      // Scale the spin by the on-screen size so a drag feels the same
      // whether the field is 200px on a phone or 600px on a desktop.
      const w = canvas.getBoundingClientRect().width || GW;
      tk.targetRot += dx / w * 3.2;
      tk.vel = dx / w * 0.22;
    });
    canvas.addEventListener('pointerup', e => {
      const wasDrag = moved > 6;
      tk.drag = null;
      if (!active) return;
      if (wasDrag) return;                  // a spin is not a selection
      pickAt(canvas, e.clientX, e.clientY);
    });
    canvas.addEventListener('pointercancel', () => { tk.drag = null; });
  }

  // Map a client point into the canvas's 340-unit drawing space and find the
  // nearest hit-tested node. The radius is generous and floored at 22px of
  // *screen* space, because a 6px star is untappable with a thumb.
  function pickAt(canvas, cx, cy) {
    const box = canvas.getBoundingClientRect();
    const scale = box.width / GW;
    const x = (cx - box.left) / scale, y = (cy - box.top) / scale;
    const minR = 22 / scale;
    let best = null, bestD = Infinity;
    for (const h of tk.hit) {
      const d = Math.hypot(h.x - x, h.y - y);
      const r = Math.max(h.r + 4, minR);
      if (d < r && d < bestD) { best = h; bestD = d; }
    }
    if (best) {
      tk.selected = best.id;
      (active === 'processes' ? showProcCard : showMemCard)(best.data);
    } else {
      tk.selected = null;
      hideCard();
    }
  }

  function tkClear() {
    tk.ctx.clearRect(0, 0, GW, GW);
  }

  // ══════════════════════════════════════════════════════════════════
  //  PROCESS CONSTELLATION
  // ══════════════════════════════════════════════════════════════════
  //
  // Layout is phyllotactic — the golden angle between successive bodies,
  // radius as sqrt(i/n). It is the sunflower-seed packing, and it is the
  // right choice here for a practical reason rather than a decorative one:
  // it distributes n points over a disc with no clumping and no visible
  // rings or spokes for any n, so the field stays readable as processes
  // appear and disappear between polls.
  //
  // Rank drives radius, so the heaviest process is always at the centre and
  // your eye lands on it before you have read a single label.

  const GOLDEN = Math.PI * (3 - Math.sqrt(5));

  const procs = {
    rows: [], total: 0, cpuTotal: 0, cpuCount: 1, memTotal: 0,
    timer: 0, pos: new Map(), lastErr: '',
  };

  async function procsPoll() {
    try {
      const r = await fetch('/processes', { cache: 'no-store' });
      const d = await r.json();
      procs.rows = d.procs || [];
      procs.total = d.total || 0;
      procs.cpuTotal = d.cpu_total || 0;
      procs.cpuCount = d.cpu_count || 1;
      procs.memTotal = d.mem_total || 0;
      procs.lastErr = d.error || '';
      setLabel(`${d.shown || 0} OF ${procs.total} PROCESSES · ${procs.cpuTotal.toFixed(0)}% CPU`);
      // Keep the open card live rather than frozen at its opening values.
      if (tk.selected && String(tk.selected).startsWith('p:')) {
        const pid = parseInt(String(tk.selected).slice(2), 10);
        const row = procs.rows.find(p => p.pid === pid);
        if (row) showProcCard(row);
      }
    } catch (e) {
      procs.lastErr = 'unreachable';
    }
  }

  function procsEnter() {
    procs.pos.clear();
    procsPoll();
    // 2 s is the useful floor: psutil's cpu_percent is a delta since the
    // previous read, so polling faster just divides the same work into
    // noisier samples.
    procs.timer = setInterval(procsPoll, 2000);
  }

  function procsExit() {
    clearInterval(procs.timer); procs.timer = 0;
  }

  function drawProcesses(ts) {
    const c = tk.ctx;
    tkClear();
    const rows = procs.rows;
    const n = rows.length;
    tk.hit.length = 0;
    if (!n) {
      c.fillStyle = rgba(0.5);
      c.font = '10px var(--mono, monospace)';
      c.textAlign = 'center';
      c.fillText(procs.lastErr ? procs.lastErr.toUpperCase() : 'READING PROCESS TABLE…', GCX, GCY);
      c.textAlign = 'left';
      return;
    }

    const enter = easeOut((ts - tk.enterAt) / 620);
    const spin = tk.rot * 0.35;
    const maxR = GR * 1.02;
    const acc = accent();

    // Field lines first, behind the bodies: each body tethered to the core.
    c.lineWidth = 0.5;
    c.strokeStyle = rgba(0.10 * enter);
    c.beginPath();

    const placed = [];
    for (let i = 0; i < n; i++) {
      const row = rows[i];
      const ang = i * GOLDEN + spin;
      const rad = Math.sqrt((i + 0.6) / n) * maxR * enter;
      const x = GCX + Math.cos(ang) * rad;
      const y = GCY + Math.sin(ang) * rad * 0.94;   // slight squash = a tilted plane
      // Area, not radius, tracks memory: doubling a radius quadruples the
      // ink and lies about the ratio.
      const memFrac = procs.memTotal ? row.rss / procs.memTotal : 0;
      const r = Math.max(2.2, Math.min(15, Math.sqrt(memFrac) * 46));
      const heat = Math.min(1, row.cpu / (procs.cpuCount * 45));
      placed.push({ row, x, y, r, heat, i });
      c.moveTo(GCX, GCY); c.lineTo(x, y);
    }
    c.stroke();

    // Bodies, coolest first so the hot ones paint over them.
    placed.sort((a, b) => a.heat - b.heat);
    for (const p of placed) {
      const { row, x, y, r, heat } = p;
      const sel = tk.selected === 'p:' + row.pid;
      // Hot processes breathe. It is not decoration: motion is the channel
      // your peripheral vision actually notices, so the thing pegging a core
      // catches your eye from across the room.
      const pulse = heat > 0.06 ? 1 + Math.sin(ts / 320 + p.i) * 0.10 * heat : 1;
      const rr = r * pulse;

      if (heat > 0.02) {
        const g = c.createRadialGradient(x, y, 0, x, y, rr * 4.2);
        g.addColorStop(0, `rgba(255,${Math.round(210 - heat * 150)},${Math.round(120 - heat * 110)},${0.30 * heat + 0.05})`);
        g.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = g;
        c.beginPath(); c.arc(x, y, rr * 4.2, 0, TAU); c.fill();
      }

      // Warm for CPU, accent for idle — one glance separates "busy" from
      // "big but asleep", which is the distinction the whole view exists for.
      const col = heat > 0.02
        ? [255, Math.round(206 - heat * 130), Math.round(126 - heat * 106)]
        : acc;
      c.fillStyle = `rgba(${col[0]},${col[1]},${col[2]},${0.30 + 0.62 * Math.max(heat, 0.12)})`;
      c.beginPath(); c.arc(x, y, rr, 0, TAU); c.fill();

      c.lineWidth = sel ? 1.6 : 0.7;
      c.strokeStyle = sel ? 'rgba(230,246,255,0.95)'
                          : `rgba(${col[0]},${col[1]},${col[2]},0.75)`;
      c.stroke();

      if (sel) {
        c.lineWidth = 0.8;
        c.strokeStyle = 'rgba(230,246,255,0.42)';
        c.beginPath(); c.arc(x, y, rr + 6 + Math.sin(ts / 260) * 1.4, 0, TAU); c.stroke();
      }

      tk.hit.push({ id: 'p:' + row.pid, x, y, r: rr, data: row });
    }

    // Labels only for the heaviest few. Every body labelled is a hairball;
    // the rest are one tap away.
    //
    // They are pushed radially outward rather than sat on top of the node,
    // because rank drives radius: the busiest processes are the ones nearest
    // the centre, so their labels are exactly the ones that would collide
    // with each other and with the load readout. Outward plus a minimum
    // separation keeps the four that matter readable.
    c.font = '600 6px ui-monospace, monospace';
    c.textAlign = 'center';
    const top = placed.slice().sort((a, b) => b.heat - a.heat).slice(0, 4);
    const put = [];
    for (const p of top) {
      if (p.heat < 0.03) continue;
      const dx = p.x - GCX, dy = p.y - GCY;
      const m = Math.hypot(dx, dy) || 1;
      const out = Math.max(m + p.r + 10, 46);
      const lx = GCX + dx / m * out, ly = GCY + dy / m * out;
      if (put.some(q => Math.hypot(q.x - lx, q.y - ly) < 21)) continue;
      put.push({ x: lx, y: ly });
      c.strokeStyle = rgba(0.20 * enter);
      c.lineWidth = 0.4;
      c.beginPath();
      c.moveTo(p.x + dx / m * (p.r + 1.5), p.y + dy / m * (p.r + 1.5));
      c.lineTo(lx - dx / m * 4, ly - dy / m * 4);
      c.stroke();
      c.fillStyle = `rgba(235,247,255,${0.34 + 0.5 * enter})`;
      c.fillText(p.row.name.slice(0, 15), lx, ly + 2.2);
    }
    c.textAlign = 'left';

    // Core: total load.
    const coreR = 16 + procs.cpuTotal / 100 * 7;
    const cg = c.createRadialGradient(GCX, GCY, 0, GCX, GCY, coreR * 2.4);
    cg.addColorStop(0, rgba(0.55 * enter, acc));
    cg.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = cg;
    c.beginPath(); c.arc(GCX, GCY, coreR * 2.4, 0, TAU); c.fill();
    c.fillStyle = `rgba(235,247,255,${0.9 * enter})`;
    c.font = '600 13px ui-sans-serif, system-ui, sans-serif';
    c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillText(procs.cpuTotal.toFixed(0) + '%', GCX, GCY);
    c.textAlign = 'left'; c.textBaseline = 'alphabetic';
  }

  function showProcCard(row) {
    const esc = s => String(s).replace(/[&<>"]/g, m =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
    const html = `
      <div class="tk-card-kicker">PROCESS</div>
      <div class="tk-card-title">${esc(row.name)}</div>
      <div class="tk-card-grid">
        <div><b>PID</b><span>${row.pid}</span></div>
        <div><b>CPU</b><span>${row.cpu.toFixed(1)}%</span></div>
        <div><b>MEMORY</b><span>${fmtBytes(row.rss)}</span></div>
        <div><b>USER</b><span>${esc(row.user || '—')}</span></div>
      </div>`;
    showCard(html, [
      { label: 'TERMINATE', run: () => killPid(row.pid, false, row.name) },
      { label: 'FORCE KILL', danger: true, run: () => killPid(row.pid, true, row.name) },
    ]);
  }

  async function killPid(pid, force, name) {
    try {
      const r = await fetch('/proc_kill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pid, force }),
      });
      const d = await r.json();
      if (ctxRef && ctxRef.log) {
        ctxRef.log(d.ok ? `Sent ${d.signal} to ${name} (${pid})`
                        : `Kill failed: ${d.error}`, !!d.ok);
      }
      if (d.ok) { tk.selected = null; hideCard(); }
      else showCard(`<div class="tk-card-kicker">FAILED</div>
                     <div class="tk-card-title">${name}</div>
                     <div class="tk-card-note">${d.error}</div>`, []);
      procsPoll();
    } catch (e) {
      if (ctxRef && ctxRef.log) ctxRef.log('Kill request failed', false);
    }
  }

  // ══════════════════════════════════════════════════════════════════
  //  MEMORY CONSTELLATION
  // ══════════════════════════════════════════════════════════════════
  //
  // The backend has already done the hard part: every memory's 384-d
  // embedding is projected onto a unit sphere and handed over as a lat/lon.
  // So this is the globe's own orthographic drop applied to stars instead of
  // coastlines, which is exactly why the projection was worth reusing —
  // spinning the memory of your assistant feels like the same object as
  // spinning the planet, because it is.

  const mem = {
    nodes: [], edges: [], xyz: null, counts: null,
    loading: false, err: '', poll: 0, focus: new Set(),
  };

  async function memLoad() {
    if (mem.loading) return;
    mem.loading = true;
    try {
      const r = await fetch('/memory_graph', { cache: 'no-store' });
      const d = await r.json();
      if (d.building) {
        setLabel('PROJECTING MEMORY…');
        // The first build is a couple of seconds of SVD on a background
        // thread; check back rather than blocking the server for it.
        mem.poll = setTimeout(() => { mem.loading = false; memLoad(); }, 900);
        return;
      }
      mem.nodes = d.nodes || [];
      mem.edges = d.edges || [];
      mem.counts = d.counts || null;
      mem.err = d.error || '';
      // Unit vectors once, so a frame is a rotation and a drop.
      const n = mem.nodes.length;
      const xyz = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) {
        const la = mem.nodes[i].lat * D2R, lo = mem.nodes[i].lon * D2R;
        const cl = Math.cos(la);
        xyz[i * 3] = cl * Math.sin(lo);
        xyz[i * 3 + 1] = Math.sin(la);
        xyz[i * 3 + 2] = cl * Math.cos(lo);
      }
      mem.xyz = xyz;
      if (mem.counts) {
        setLabel(`${n} MEMORIES · ${mem.counts.knowledge} KNOWN · ${mem.counts.episodes} LIVED`);
      } else if (mem.err) {
        setLabel(mem.err.toUpperCase());
      }
    } catch (e) {
      mem.err = 'memory graph unreachable';
      setLabel('MEMORY GRAPH UNREACHABLE');
    } finally {
      mem.loading = false;
    }
  }

  function memEnter() {
    if (!mem.nodes.length) { setLabel('PROJECTING MEMORY…'); memLoad(); }
    else setLabel(`${mem.nodes.length} MEMORIES · ${mem.counts ? mem.counts.knowledge : 0} KNOWN · ${mem.counts ? mem.counts.episodes : 0} LIVED`);
  }

  function memExit() { clearTimeout(mem.poll); }

  // Pushed apart deliberately: at a glance you should be able to tell the
  // things she was *told* from the things she *did*, and the first pass had
  // both washing out to the same pale blue once the glows stacked in the
  // dense regions.
  const KNOWLEDGE_RGB = [ 86, 194, 255];   // told
  const EPISODE_RGB   = [196, 108, 255];   // did

  function drawMemory(ts) {
    const c = tk.ctx;
    tkClear();
    const n = mem.nodes.length;
    tk.hit.length = 0;
    if (!n) {
      c.fillStyle = rgba(0.5);
      c.font = '10px ui-monospace, monospace';
      c.textAlign = 'center';
      c.fillText(mem.err ? mem.err.toUpperCase() : 'PROJECTING MEMORY…', GCX, GCY);
      c.textAlign = 'left';
      return;
    }

    const enter = easeOut((ts - tk.enterAt) / 700);
    const cr = Math.cos(tk.rot), sr = Math.sin(tk.rot);
    const ct = Math.cos(tk.tilt), st = Math.sin(tk.tilt);
    const R = GR * enter;

    // Faint sphere so the stars read as sitting *on* something.
    const shell = c.createRadialGradient(GCX - R * 0.3, GCY - R * 0.35, R * 0.1, GCX, GCY, R);
    shell.addColorStop(0, rgba(0.055 * enter));
    shell.addColorStop(1, 'rgba(0,0,0,0)');
    c.fillStyle = shell;
    c.beginPath(); c.arc(GCX, GCY, R, 0, TAU); c.fill();
    c.strokeStyle = rgba(0.13 * enter);
    c.lineWidth = 0.6;
    c.beginPath(); c.arc(GCX, GCY, R, 0, TAU); c.stroke();

    // Project every node once per frame into scratch arrays.
    const xyz = mem.xyz;
    const px = new Float32Array(n), py = new Float32Array(n), pz = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const x = xyz[i * 3], y = xyz[i * 3 + 1], z = xyz[i * 3 + 2];
      const x1 = x * cr + z * sr;
      const z1 = z * cr - x * sr;
      const y2 = y * ct - z1 * st;
      px[i] = GCX + x1 * R;
      py[i] = GCY - y2 * R;
      pz[i] = y * st + z1 * ct;          // >0 = facing us
    }

    // Edges under the stars, only when both ends are on the near face —
    // a chord through the sphere to a star you cannot see reads as noise.
    c.lineWidth = 0.45;
    for (const [a, b, score] of mem.edges) {
      if (pz[a] <= 0.02 || pz[b] <= 0.02) continue;
      const depth = Math.min(pz[a], pz[b]);
      const alpha = (score - 0.42) * 0.62 * depth * enter;
      if (alpha <= 0.012) continue;
      c.strokeStyle = rgba(alpha);
      c.beginPath(); c.moveTo(px[a], py[a]); c.lineTo(px[b], py[b]); c.stroke();
    }

    // Stars, far ones first so near ones overlap correctly.
    const order = Array.from({ length: n }, (_, i) => i).sort((a, b) => pz[a] - pz[b]);
    for (const i of order) {
      const z = pz[i];
      if (z <= -0.05) continue;
      const node = mem.nodes[i];
      const near = Math.max(0, z);
      const col = node.kind === 'episode' ? EPISODE_RGB : KNOWLEDGE_RGB;
      const sel = tk.selected === node.id;
      const focused = mem.focus.has(node.id);
      // Twinkle keyed off the index so neighbours are out of phase.
      const tw = 0.86 + Math.sin(ts / 900 + i * 1.7) * 0.14;
      let r = (1.35 + near * 1.75) * tw * enter;
      let a = (0.16 + near * 0.72) * enter;
      if (focused) { r *= 1.9; a = Math.min(1, a + 0.3); }

      if (near > 0.55 || focused) {
        const g = c.createRadialGradient(px[i], py[i], 0, px[i], py[i], r * 3.6);
        g.addColorStop(0, `rgba(${col[0]},${col[1]},${col[2]},${a * 0.26})`);
        g.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = g;
        c.beginPath(); c.arc(px[i], py[i], r * 3.6, 0, TAU); c.fill();
      }

      c.fillStyle = sel ? 'rgba(240,250,255,0.98)'
                        : `rgba(${col[0]},${col[1]},${col[2]},${a})`;
      c.beginPath(); c.arc(px[i], py[i], sel ? r * 2.1 : r, 0, TAU); c.fill();

      if (sel) {
        c.strokeStyle = 'rgba(235,247,255,0.5)';
        c.lineWidth = 0.8;
        c.beginPath();
        c.arc(px[i], py[i], r * 2.1 + 5 + Math.sin(ts / 260) * 1.5, 0, TAU);
        c.stroke();
      }

      if (near > 0.12) tk.hit.push({ id: node.id, x: px[i], y: py[i], r, data: node });
    }
  }

  function showMemCard(node) {
    const esc = s => String(s).replace(/[&<>"]/g, m =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));
    const kind = node.kind === 'episode' ? 'EPISODE' : 'KNOWLEDGE';
    const when = fmtAgo(node.ts);
    const src = node.source ? `<div><b>SOURCE</b><span>${esc(node.source)}</span></div>` : '';
    showCard(`
      <div class="tk-card-kicker">${kind}${when ? ' · ' + when : ''}</div>
      <div class="tk-card-note">${esc(node.text || '(empty)')}</div>
      <div class="tk-card-grid">${src}</div>`, []);
  }

  // ══════════════════════════════════════════════════════════════════
  //  DRIVER
  // ══════════════════════════════════════════════════════════════════

  // Nothing here paints between takeovers now that the cover has moved off
  // the orb, so the loop starts on show() and lets itself die once the field
  // is gone, rather than burning a frame callback all session on an empty
  // canvas.
  function startFrames() {
    if (!rafId) rafId = requestAnimationFrame(frame);
  }

  function frame(ts) {
    if (!active) { rafId = 0; return; }

    if (!tk.drag) {
      // Momentum after a flick, then a slow idle drift so the field never
      // looks frozen.
      tk.targetRot += tk.vel;
      tk.vel *= 0.94;
      if (Math.abs(tk.vel) < 0.0004) { tk.vel = 0; tk.targetRot += 0.0016; }
    }
    tk.rot += (tk.targetRot - tk.rot) * 0.10;
    if (active === 'processes') drawProcesses(ts);
    else if (active === 'memory') drawMemory(ts);

    rafId = requestAnimationFrame(frame);
  }

  let hideTimer = 0;
  const HOLD_MS = 45000;      // longer than the globe's: these are for reading

  function show(kind) {
    if (!ctxRef) return;
    if (active === kind) { arm(); return; }
    if (active) exitCurrent();
    // The globe drives the same --globe-scale these do, at equal specificity.
    // Leaving it up means two takeovers fighting over one transform and both
    // canvases drawing at once, so whoever is asked for second wins outright.
    if (ctxRef.hideGlobe) ctxRef.hideGlobe();
    active = kind;
    startFrames();
    tk.enterAt = performance.now();
    tk.selected = null;
    tk.vel = 0;
    hideCard();
    fit();
    ctxRef.orbWrap.dataset.takeover = kind;
    document.body.dataset.takeover = kind;
    if (kind === 'processes') procsEnter();
    if (kind === 'memory') memEnter();
    arm();
  }

  function arm() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, HOLD_MS);
  }

  function exitCurrent() {
    if (active === 'processes') procsExit();
    if (active === 'memory') memExit();
  }

  function hide() {
    clearTimeout(hideTimer);
    if (!active) return;
    exitCurrent();
    const wrap = ctxRef.orbWrap;
    wrap.dataset.takeover = '';
    delete document.body.dataset.takeover;
    hideCard();
    setTimeout(() => {
      if (wrap.dataset.takeover) return;      // something else took over mid-fade
      active = null;
      tkClear();
      setLabel('');
      delete wrap.dataset.takeover;
    }, 780);
  }

  // ══════════════════════════════════════════════════════════════════
  //  PUBLIC
  // ══════════════════════════════════════════════════════════════════

  window.NORA_TAKEOVERS = {
    init(context) {
      ctxRef = context;
      tkInit(context.takeCanvas);
      addEventListener('resize', () => { if (active) fit(); });
    },
    show, hide, fit,
    active: () => active,
    // Highlight the memories a recall matched, so the constellation answers
    // "where did that come from" instead of just looking pretty.
    focusMemories(ids) {
      mem.focus = new Set(ids || []);
      if (mem.focus.size) show('memory');
    },
  };
})();
