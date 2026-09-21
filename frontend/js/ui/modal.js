/* B3 Hive — modal focus management.

   Alpine core ships without the focus plugin, so the trap is implemented by
   hand. Requirements (brief §8.5): role=dialog, aria-modal, focus trap, ESC
   to close, backdrop click to close, and focus RESTORED to whatever was
   focused before the dialog opened.

   Usage from markup:
     <div class="backdrop" x-init="trapFocus($el, () => closeThing())"> … </div>

   Cleanup is automatic: a MutationObserver notices when Alpine's x-if removes
   the element and restores focus without the close path having to remember. */

const FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])', 'summary',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

let openCount = 0;
// Open dialogs, oldest first. Only the LAST one may pull focus back: with two
// (e.g. the unlock prompt raised over an action dialog) each trap would drag
// focus into its own dialog and they would ping-pong until the stack overflowed.
const traps = [];

export function trapFocus(el, onEscape) {
  if (!el || el.__b3trapped) return;
  el.__b3trapped = true;

  const previous = document.activeElement;
  openCount += 1;
  traps.push(el);
  // Stop the page behind the dialog from scrolling under it.
  document.documentElement.style.overflow = 'hidden';

  const focusables = () => Array.from(el.querySelectorAll(FOCUSABLE))
    .filter((n) => n.offsetParent !== null || n === document.activeElement);

  // Prefer an explicit [autofocus], then the first field, then the dialog.
  requestAnimationFrame(() => {
    const target = el.querySelector('[autofocus]')
      || el.querySelector('input:not([type="hidden"]), textarea, select')
      || focusables()[0]
      || el;
    try { target.focus({ preventScroll: true }); } catch { /* ignore */ }
  });

  const onKeydown = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onEscape?.();
      return;
    }
    if (e.key !== 'Tab') return;
    const items = focusables();
    if (!items.length) { e.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || !el.contains(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  };
  el.addEventListener('keydown', onKeydown);

  // Focus could still escape via programmatic focus or browser chrome; pull
  // it back when it lands outside an open dialog.
  const onFocusIn = (e) => {
    if (!el.isConnected || traps[traps.length - 1] !== el) return;
    if (!el.contains(e.target)) {
      const items = focusables();
      if (items.length) items[0].focus();
    }
  };
  document.addEventListener('focusin', onFocusIn);

  const cleanup = () => {
    el.removeEventListener('keydown', onKeydown);
    document.removeEventListener('focusin', onFocusIn);
    observer.disconnect();
    el.__b3trapped = false;
    const at = traps.indexOf(el);
    if (at !== -1) traps.splice(at, 1);
    openCount = Math.max(0, openCount - 1);
    if (openCount === 0) document.documentElement.style.overflow = '';
    if (previous && previous.isConnected) {
      try { previous.focus({ preventScroll: true }); } catch { /* ignore */ }
    }
  };

  const observer = new MutationObserver(() => {
    if (!el.isConnected) cleanup();
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

export const modalMixin = {
  trapFocus,

  /** Backdrop click closes, but only when the click started on the backdrop
   *  itself — dragging to select text inside the dialog must not dismiss it. */
  backdropClose(event, close) {
    if (event.target === event.currentTarget) close();
  },
};
