/* B3 Hive — toasts.

   A stack rather than the previous single slot, so a burst of results (e.g. a
   batch run broadcasting several transactions) does not overwrite itself.
   role=status + aria-live announce to screen readers; every toast pairs its
   colour with an icon and a word, never colour alone. */

let seq = 0;

const ICONS = {
  success: 'check-circle',
  danger:  'alert-circle',
  warning: 'alert',
  info:    'info',
};

export const toastMixin = {

  /** showToast(message) | showToast(message, 'danger') */
  showToast(message, type = 'success', opts = {}) {
    if (!message) return;
    const id = ++seq;
    const toast = {
      id,
      message: String(message),
      type: ICONS[type] ? type : 'info',
      icon: ICONS[type] || 'info',
      action: opts.action || null,
    };
    this.toasts.push(toast);

    // Errors linger; confirmations get out of the way.
    const ttl = opts.duration ?? (type === 'danger' ? 7000 : 4000);
    setTimeout(() => this.dismissToast(id), ttl);
  },

  dismissToast(id) {
    const i = this.toasts.findIndex((t) => t.id === id);
    if (i !== -1) this.toasts.splice(i, 1);
  },

  /** Standard failure path for every action: plain language, never raw RPC.
   *  Cancelling a point-of-action unlock is silent — it was deliberate. */
  reportError(err, fallback = 'That did not work.') {
    if (err && err.cancelled) return;
    this.showToast((err && err.message) || fallback, 'danger');
  },

  async copyText(text, label = 'Copied') {
    const value = String(text ?? '');
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      this.showToast(label);
    } catch {
      // Clipboard API needs a secure context; fall back to a hidden textarea.
      try {
        const ta = document.createElement('textarea');
        ta.value = value;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
        this.showToast(label);
      } catch {
        this.showToast('Could not copy — select the text and copy manually.', 'warning');
      }
    }
  },
};
