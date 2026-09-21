/* B3 Hive — the Send flow.

   Three steps instead of one long form: To -> Amount -> Review. The old UI
   put every field and the preview on one screen, which made the irreversible
   step feel like just another button.

   Two-phase safety is the backend's (wallet.py `send`): confirm=false builds,
   signs and testmempoolaccept-checks WITHOUT broadcasting; confirm=true
   re-derives the identical transaction and broadcasts. Entering Review runs
   phase one, so the review shows the real txid and the real mempool verdict
   before the user commits. */

import {
  isValidAddress, addressProblem, validateAmountInput, subtractAmounts,
  compareAmounts, fmtAmount, truncAddr,
} from '../core/format.js';

/** Held back from "Max" so there is always something left for the fee.
 *  A 1-in/2-out P2PKH transaction is ~226 vB; at the relay floor this is
 *  several orders of magnitude more than needed, and the change returns to
 *  the wallet anyway. */
const FEE_RESERVE = '0.001000000';

export const sendMixin = {

  addressProblem,
  isValidAddress,

  resetSend(keep) {
    if (keep) return;
    this.send = {
      step: 1, to: '', amount: '', label: '',
      busy: false, err: '',
      preview: null, result: null,
      pickerOpen: false, pickerQuery: '', pickerTab: 'contacts',
    };
  },

  /** Jump straight into the flow pre-filled (palette, address book). */
  sendTo(address, label) {
    this.go('send');
    this.send.to = address;
    this.send.label = label || '';
    this.send.step = 2;
    this.$nextTick(() => document.getElementById('send-amount')?.focus());
  },

  /* ----------------------------------------------------------- validation */

  toValid() { return isValidAddress(this.send.to); },

  toFeedback() {
    const v = this.send.to.trim();
    if (!v) return null;
    if (isValidAddress(v)) {
      const label = this.contactFor(v)?.label || this.labelFor(v);
      return { ok: true, text: label ? 'Valid address — ' + label : 'Valid B3 address' };
    }
    return { ok: false, text: addressProblem(v) };
  },

  /** Spendable = confirmed balance only. Pending and staked coins cannot
   *  be spent, and saying so is better than an unexplained failure. */
  spendable() { return this.wallet.balance ?? '0'; },

  maxSendable() {
    const s = subtractAmounts(this.spendable(), FEE_RESERVE);
    if (!s || s.startsWith('-')) return '0';
    return s;
  },

  fillMax() {
    this.send.amount = this.maxSendable().replace(/0+$/, '').replace(/\.$/, '');
    this.send.err = '';
  },

  amountCheck() {
    return validateAmountInput(this.send.amount, { max: this.spendable() });
  },

  amountValid() { return this.amountCheck().ok; },

  amountFeedback() {
    if (!this.send.amount.trim()) return null;
    const c = this.amountCheck();
    if (!c.ok) return { ok: false, text: c.error };
    const after = subtractAmounts(this.spendable(), this.send.amount);
    if (after && !after.startsWith('-')) {
      return { ok: true, text: fmtAmount(after, { unit: true, maxDecimals: 4 }) + ' left afterwards' };
    }
    return { ok: true, text: null };
  },

  overSpendable() {
    if (!this.send.amount.trim()) return false;
    return compareAmounts(this.send.amount, this.spendable()) > 0;
  },

  /* ---------------------------------------------------------- navigation */

  sendNext() {
    if (this.send.step === 1) {
      if (!this.toValid()) { this.send.err = addressProblem(this.send.to); return; }
      this.send.err = '';
      this.send.label = this.labelFor(this.send.to.trim()) || '';
      this.send.step = 2;
      this.$nextTick(() => document.getElementById('send-amount')?.focus());
      return;
    }
    if (this.send.step === 2) {
      const c = this.amountCheck();
      if (!c.ok) { this.send.err = c.error; return; }
      this.send.err = '';
      this.buildPreview();
    }
  },

  sendBack() {
    if (this.send.step === 3) { this.send.preview = null; }
    this.send.step = Math.max(1, this.send.step - 1);
    this.send.err = '';
  },

  /* ------------------------------------------------------ phase 1: preview */

  async buildPreview() {
    this.send.busy = true; this.send.err = ''; this.send.preview = null;
    try {
      const p = await this.api('/api/wallet/send', {
        method: 'POST',
        body: JSON.stringify({
          recipients: [{ address: this.send.to.trim(), amount: this.send.amount.trim() }],
          confirm: false,
        }),
      });
      this.send.preview = p;
      this.send.step = 3;
      if (!p.mempool_ok) {
        this.send.err = this.rejectionText(p.rejection);
      }
    } catch (e) {
      // A cancelled unlock prompt is not an error worth shouting about.
      if (!e.cancelled) this.send.err = e.message;
    }
    this.send.busy = false;
  },

  /** Translate mempool reject reasons into something actionable. */
  rejectionText(reason) {
    const r = String(reason || '').toLowerCase();
    if (r.includes('insufficient')) return 'Not enough spendable B3 for this amount plus the fee.';
    if (r.includes('dust')) return 'That amount is too small for the network to relay.';
    if (r.includes('fee')) return 'The fee this transaction would pay is too low for the network right now.';
    if (r.includes('already')) return 'An identical transaction is already in the queue.';
    return 'The network would not accept this transaction. Try a different amount.';
  },

  /* ------------------------------------------------ phase 2: broadcast it */

  confirmSend() {
    const amountText = fmtAmount(this.send.amount, { unit: true });
    const dest = this.send.to.trim();
    this.confirm({
      title: 'Send ' + amountText + '?',
      body: 'This pays ' + amountText + ' to ' + truncAddr(dest)
          + (this.send.label ? ' (' + this.send.label + ')' : '')
          + '. Once broadcast it cannot be undone, cancelled or reversed — '
          + 'there is no way to get the coins back if the address is wrong.',
      detail: [
        { key: 'Amount', value: amountText },
        { key: 'To', value: dest, mono: true },
        ...(this.send.label ? [{ key: 'Known as', value: this.send.label }] : []),
      ],
      confirmLabel: 'Send now',
      danger: true,
      run: () => this.broadcast(),
    });
  },

  async broadcast() {
    this.send.busy = true; this.send.err = '';
    try {
      const r = await this.api('/api/wallet/send', {
        method: 'POST',
        body: JSON.stringify({
          recipients: [{ address: this.send.to.trim(), amount: this.send.amount.trim() }],
          confirm: true,
        }),
      });
      this.send.result = r;
      this.send.step = 4;
      this.showToast('Sent — transaction broadcast');
      this.loadBalances();
      this.loadHistory();
    } catch (e) {
      if (!e.cancelled) {
        this.send.err = e.message;
        this.reportError(e);
      }
    }
    this.send.busy = false;
  },

  sendAgain() {
    this.resetSend();
    this.$nextTick(() => document.getElementById('send-to')?.focus());
  },

  /* ------------------------------------------------------ address picker */

  pickerResults() {
    const q = this.send.pickerQuery.trim().toLowerCase();
    let list = (this.book.addresses || []);
    if (q) {
      list = list.filter((a) =>
        (a.label || '').toLowerCase().includes(q) || a.address.toLowerCase().includes(q));
    }
    return list.slice(0, 40);
  },

  pickAddress(entry) {
    this.send.to = entry.address;
    this.send.label = entry.label || '';
    this.send.pickerOpen = false;
    this.send.err = '';
  },

  async pasteInto(field) {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) return;
      if (field === 'to') { this.send.to = text; this.send.err = ''; }
    } catch {
      this.showToast('Clipboard is not available — paste with your keyboard', 'warning');
    }
  },
};
