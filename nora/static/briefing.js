// The briefing takeover — current headlines as a card grid.
//
// The other takeovers are canvases: a constellation is geometry, and geometry
// belongs in a canvas. This one is text and photographs, so it is DOM. It
// still lives outside .orb-wrap for the reason #tk-label does — the wrap gets
// scaled 2-4x, and a headline dragged along by that transform ends up either
// enormous or blurry.
//
// The cards are handed over whole by the backend in the takeover payload,
// already fetched, ranked and deduped. This file never fetches: what is drawn
// here is exactly the list NORA was summarising while it appeared, and a
// second request would let the picture and the voice drift apart.

(function () {
  'use strict';

  let root = null, ctxRef = null;
  let hideTimer = 0, tickTimer = 0;
  let holdMs = 45000, expiresAt = 0;
  let onDismiss = null;
  let shown = false;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // "3 hours ago" beats a timestamp on a screen you look at for 40 seconds.
  function ago(epochSeconds) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
    if (mins < 2) return 'just now';
    if (mins < 60) return mins + ' min ago';
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? ' hour ago' : ' hours ago');
    const days = Math.round(hrs / 24);
    return days + (days === 1 ? ' day ago' : ' days ago');
  }

  // A headline with no picture gets a deliberate typographic panel rather
  // than a grey rectangle: Google News strips thumbnails, so for an off-list
  // topic this is the normal case, not the error case.
  function initial(source) {
    return (source || '?').trim().charAt(0).toUpperCase();
  }

  function buildCard(card, index) {
    const a = el('a', 'bf-card');
    a.href = card.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.style.setProperty('--i', index);

    const media = el('div', 'bf-media');
    if (card.image) {
      const img = el('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.src = card.image;
      // A publisher's CDN can 404 or hotlink-block. Falling back to the
      // typographic panel keeps the grid even instead of punching a hole.
      img.addEventListener('error', () => {
        media.classList.add('bf-noimg');
        media.textContent = initial(card.source);
      });
      media.appendChild(img);
    } else {
      media.classList.add('bf-noimg');
      media.textContent = initial(card.source);
    }

    const body = el('div', 'bf-body');
    const meta = el('div', 'bf-meta');
    meta.appendChild(el('span', 'bf-src', card.source || ''));
    meta.appendChild(el('span', 'bf-dot', '·'));
    meta.appendChild(el('span', 'bf-age', ago(card.published)));
    body.appendChild(meta);
    body.appendChild(el('h3', 'bf-title', card.title || ''));

    a.appendChild(media);
    a.appendChild(body);
    return a;
  }

  function ensureRoot() {
    if (root) return root;
    root = el('div', 'bf-root');
    root.id = 'bf-root';

    const head = el('div', 'bf-head');
    head.appendChild(el('span', 'bf-kicker', ''));
    const close = el('button', 'bf-close');
    close.type = 'button';
    close.innerHTML = '<span class="bf-ring"></span><span class="bf-x">DISMISS</span>';
    close.addEventListener('click', ev => { ev.stopPropagation(); hide(); });
    head.appendChild(close);

    root.appendChild(head);
    root.appendChild(el('div', 'bf-grid'));
    document.body.appendChild(root);

    // Any deliberate attention resets the clock. A hard countdown that fires
    // while you are still reading is the difference between an assistant and
    // a kitchen timer.
    ['pointerdown', 'pointermove', 'wheel', 'keydown', 'touchstart']
      .forEach(evt => root.addEventListener(evt, arm, { passive: true }));

    return root;
  }

  function arm() {
    if (!shown) return;
    clearTimeout(hideTimer);
    expiresAt = performance.now() + holdMs;
    hideTimer = setTimeout(hide, holdMs);
  }

  function tick() {
    if (!shown || !root) return;
    const left = Math.max(0, expiresAt - performance.now());
    const frac = holdMs ? left / holdMs : 0;
    root.style.setProperty('--bf-left', frac.toFixed(3));
    const secs = Math.ceil(left / 1000);
    const x = root.querySelector('.bf-x');
    // Only counts out loud in the last stretch; before that it just says
    // what the button does.
    if (x) x.textContent = secs <= 10 ? 'DISMISS ' + secs : 'DISMISS';
  }

  function show(payload) {
    ensureRoot();
    const cards = (payload && payload.cards) || [];
    if (!cards.length) return;

    holdMs = Math.max(5000, (payload && payload.hold_ms) || 45000);

    const kicker = root.querySelector('.bf-kicker');
    if (kicker) kicker.textContent = (payload.label || 'briefing').toUpperCase();

    const grid = root.querySelector('.bf-grid');
    grid.textContent = '';
    cards.forEach((c, i) => grid.appendChild(buildCard(c, i)));
    grid.dataset.count = String(cards.length);

    shown = true;
    root.hidden = false;
    document.body.dataset.briefing = '1';
    requestAnimationFrame(() => root.classList.add('in'));

    arm();
    clearInterval(tickTimer);
    tickTimer = setInterval(tick, 250);
    tick();
  }

  function hide() {
    clearTimeout(hideTimer);
    clearInterval(tickTimer);
    if (!shown) return;
    shown = false;
    if (root) root.classList.remove('in');
    delete document.body.dataset.briefing;
    setTimeout(() => {
      if (shown) return;               // shown again mid-fade
      if (root) { root.hidden = true; root.querySelector('.bf-grid').textContent = ''; }
    }, 520);
    if (onDismiss) onDismiss();
  }

  window.NORA_BRIEFING = {
    init(context) { ctxRef = context; onDismiss = context && context.onDismiss; },
    show, hide, arm,
    active: () => shown,
  };
})();
