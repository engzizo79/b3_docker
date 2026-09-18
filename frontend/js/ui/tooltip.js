/* B3 Hive — tooltips / popovers.

   Replaces the previous CSS `::before[data-tip]` hack, which had two fatal
   problems: it was clipped by any `overflow:hidden` ancestor, and it was
   hover-only, so on a phone the explanation was simply unreachable.

   This implementation:
   - renders into #layer-top (position:fixed) so nothing can clip it;
   - uses an OPAQUE background token with a border and shadow (brief §5.6:
     tooltips must be readable in both themes);
   - opens on hover AND keyboard focus on pointer devices;
   - opens on TAP on touch devices, and closes on the next tap anywhere;
   - flips above/below and clamps horizontally to stay on screen.

   Markup: any element with `data-tip="…"`. For the round hint affordance use
   `<button class="hint-btn" data-tip="…" aria-label="Explain">i</button>`. */

const MARGIN = 8;
const GAP = 10;

let layer = null;
let node = null;
let current = null;
let hideTimer = null;

function ensureNode() {
  if (!layer) layer = document.getElementById('layer-top');
  if (!layer) return null;
  if (!node) {
    node = document.createElement('div');
    node.className = 'tooltip';
    node.setAttribute('role', 'tooltip');
    node.id = 'b3-tooltip';
    layer.appendChild(node);
  }
  return node;
}

function place(anchor) {
  const tip = ensureNode();
  if (!tip) return;
  const a = anchor.getBoundingClientRect();
  const t = tip.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // Prefer above; flip below when there is not enough room.
  let top = a.top - t.height - GAP;
  if (top < MARGIN) {
    top = a.bottom + GAP;
    // If it does not fit below either, pin to whichever side has more space.
    if (top + t.height > vh - MARGIN) {
      top = a.top > vh - a.bottom
        ? Math.max(MARGIN, a.top - t.height - GAP)
        : Math.min(vh - t.height - MARGIN, a.bottom + GAP);
    }
  }

  let left = a.left + a.width / 2 - t.width / 2;
  left = Math.max(MARGIN, Math.min(left, vw - t.width - MARGIN));

  tip.style.top = Math.round(top) + 'px';
  tip.style.left = Math.round(left) + 'px';
}

function show(anchor) {
  const text = anchor.getAttribute('data-tip');
  if (!text) return;
  clearTimeout(hideTimer);
  const tip = ensureNode();
  if (!tip) return;

  current = anchor;
  tip.textContent = text;
  tip.style.top = '-9999px';
  tip.style.left = '-9999px';
  tip.classList.add('show');
  anchor.setAttribute('aria-describedby', 'b3-tooltip');
  if (anchor.classList.contains('hint-btn')) anchor.setAttribute('aria-expanded', 'true');
  // Measure after the text is in, then position.
  requestAnimationFrame(() => { if (current === anchor) place(anchor); });
}

function hide() {
  if (!node) return;
  node.classList.remove('show');
  if (current) {
    current.removeAttribute('aria-describedby');
    if (current.classList.contains('hint-btn')) current.setAttribute('aria-expanded', 'false');
  }
  current = null;
}

function anchorFrom(target) {
  return target instanceof Element ? target.closest('[data-tip]') : null;
}

export function initTooltips() {
  const hasHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  if (hasHover) {
    document.addEventListener('pointerover', (e) => {
      const a = anchorFrom(e.target);
      if (a && a !== current) show(a);
    });
    document.addEventListener('pointerout', (e) => {
      const a = anchorFrom(e.target);
      if (a && a === current) { hideTimer = setTimeout(hide, 80); }
    });
  }

  // Keyboard: always available, hover device or not.
  document.addEventListener('focusin', (e) => {
    const a = anchorFrom(e.target);
    if (a) show(a); else hide();
  });
  document.addEventListener('focusout', (e) => {
    if (anchorFrom(e.target) === current) hide();
  });

  // Touch: tap to open, tap anywhere to close. Without this, every
  // explanation in the app is unreachable on a phone.
  document.addEventListener('click', (e) => {
    const a = anchorFrom(e.target);
    if (!a) { hide(); return; }
    if (a.classList.contains('hint-btn')) {
      e.preventDefault();
      if (a === current) hide(); else show(a);
    } else if (!hasHover) {
      if (a === current) hide(); else show(a);
    }
  });

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, { passive: true, capture: true });
  window.addEventListener('resize', hide, { passive: true });
}
