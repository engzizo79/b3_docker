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
  addAmounts, compareAmounts, isPositiveAmount, fmtAmount, truncAddr,
} from '../core/format.js';

// Mirrors HARD_MAX_INPUTS in backend/app/batch_engine.py: the real ceiling
// for a standard P2PKH transaction under B3's policy (weight-limited), not
// an arbitrary UI choice. A wallet with thousands of small reward UTXOs
// genuinely needs several transactions to sweep everything — this is the
// most any single one can ever hold.
const COIN_CONTROL_MAX_INPUTS = 675;
// A more usable per-click default — 675 inputs in one go is allowed, but a
// single sweep at that size is unusual; the quick actions default lower
// and the count is always shown so picking more is one more click, not
// hidden.
const COIN_CONTROL_QUICK_COUNT = 50;

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
      // Coin control: pick exactly which outputs to spend, instead of
      // letting the wallet choose. Off (auto) by default; `selected` is
      // keyed by "txid:vout" so toggling is O(1) either way.
      coinControl: { show: false, selected: {}, query: '' },
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

  /** Spendable = confirmed balance only, or — with coin control active —
   *  exactly the total of the coins chosen. Pending and staked coins
   *  cannot be spent, and saying so is better than an unexplained failure. */
  spendable() {
    if (this.coinControlActive()) return this.coinControlTotal();
    return this.wallet.balance ?? '0';
  },

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

  /* ---------------------------------------------------- coin control --- */

  /** Outputs a plain send may actually choose: spendable, plain P2PKH.
   *  Stake/asset/metadata carriers never show up here — unstake them or
   *  use the dedicated flow for those instead. */
  coinControlEligible() {
    return (this.utxos.list || []).filter((u) => u.p2pkh && u.spendable);
  },

  /** Eligible coins narrowed by the search box (address or label). A
   *  wallet with thousands of reward outputs needs this to find anything;
   *  bulk actions below always work over the FULL eligible list, not just
   *  what search happens to be showing. */
  coinControlFiltered() {
    const q = (this.send.coinControl.query || '').trim().toLowerCase();
    const all = this.coinControlEligible();
    if (!q) return all;
    return all.filter((u) =>
      (u.address || '').toLowerCase().includes(q) || (u.label || '').toLowerCase().includes(q));
  },

  /** Rendering thousands of rows at once is a real cost for no benefit —
   *  cap what's drawn; bulk actions still see every eligible coin via
   *  coinControlEligible(), search narrows this down to find one by hand. */
  coinControlVisible() { return this.coinControlFiltered().slice(0, 300); },

  toggleCoinControlPanel() {
    this.send.coinControl.show = !this.send.coinControl.show;
    if (this.send.coinControl.show && !this.utxos.loaded) this.loadUtxos();
  },

  coinKey(u) { return u.txid + ':' + u.vout; },

  isCoinSelected(u) { return !!this.send.coinControl.selected[this.coinKey(u)]; },

  toggleCoin(u) {
    const sel = { ...this.send.coinControl.selected };
    const k = this.coinKey(u);
    if (sel[k]) delete sel[k]; else sel[k] = u;
    this.send.coinControl.selected = sel;
    // The chosen pool just changed size — an amount that fit before may
    // not now (or Max needs to grow); re-validate instead of leaving a
    // stale error on screen.
    this.send.err = '';
  },

  coinControlActive() {
    return Object.keys(this.send.coinControl.selected).length > 0;
  },

  coinControlCount() { return Object.keys(this.send.coinControl.selected).length; },

  coinControlTotal() {
    return Object.values(this.send.coinControl.selected)
      .reduce((acc, u) => addAmounts(acc, u.amount), '0.000000000');
  },

  clearCoinControl() { this.send.coinControl.selected = {}; },

  /** True once the wallet holds more small outputs than a single
   *  transaction can carry — the bulk-select tools exist for this case
   *  (e.g. thousands of small staking-reward outputs across many
   *  addresses), not for a handful of coins someone can just click. */
  coinControlNeedsBulkTools() {
    return this.coinControlEligible().length > COIN_CONTROL_QUICK_COUNT;
  },

  coinControlMaxInputs() { return COIN_CONTROL_MAX_INPUTS; },
  coinControlQuickCount() { return COIN_CONTROL_QUICK_COUNT; },

  /** Replace the selection with the N smallest (default) or largest
   *  eligible coins. Smallest-first is the sweep-my-dust-rewards case;
   *  largest-first reaches a target amount in the fewest inputs. Always
   *  capped at COIN_CONTROL_MAX_INPUTS (the real per-transaction ceiling)
   *  even if asked for more. */
  selectCoinsBulk(order, count) {
    const n = Math.max(1, Math.min(count || COIN_CONTROL_QUICK_COUNT, COIN_CONTROL_MAX_INPUTS));
    const pool = [...this.coinControlEligible()].sort((a, b) =>
      order === 'largest' ? compareAmounts(b.amount, a.amount) : compareAmounts(a.amount, b.amount));
    const picked = pool.slice(0, n);
    const sel = {};
    for (const u of picked) sel[this.coinKey(u)] = u;
    this.send.coinControl.selected = sel;
    this.send.err = '';
    if (picked.length < pool.length) {
      this.showToast(picked.length + ' of ' + pool.length + ' coins selected ('
        + (order === 'largest' ? 'largest' : 'smallest') + ' first)');
    }
  },

  /** Select the fewest, largest coins that cover the typed amount plus a
   *  small fee margin — the "just make this payment work" button, so
   *  nobody has to hand-count outputs to reach an amount. */
  selectCoinsForAmount() {
    const target = (this.send.amount || '').trim();
    if (!isPositiveAmount(target)) {
      this.showToast('Enter an amount first', 'warning');
      return;
    }
    const need = addAmounts(target, '0.000100000'); // margin; the backend computes the real fee
    const pool = [...this.coinControlEligible()].sort((a, b) => compareAmounts(b.amount, a.amount));
    const picked = [];
    let total = '0.000000000';
    for (const u of pool) {
      if (picked.length >= COIN_CONTROL_MAX_INPUTS) break;
      picked.push(u);
      total = addAmounts(total, u.amount);
      if (compareAmounts(total, need) >= 0) break;
    }
    const sel = {};
    for (const u of picked) sel[this.coinKey(u)] = u;
    this.send.coinControl.selected = sel;
    this.send.err = '';
    if (compareAmounts(total, need) < 0) {
      this.showToast('Your spendable coins don’t add up to that amount', 'warning');
    } else {
      this.showToast(picked.length + ' coin' + (picked.length === 1 ? '' : 's') + ' selected — '
        + fmtAmount(total, { unit: true }));
    }
  },

  /** From the Tools > Coin control table: jump into Send with the checked
   *  coins already selected, instead of just having displayed them. Only
   *  `send.coinControl` survives the trip — everything else about the
   *  flow (address, amount, any earlier preview) starts fresh. */
  sendWithSelectedCoins() {
    if (!this.coinControlActive()) {
      this.showToast('Select at least one coin first', 'warning');
      return;
    }
    const selected = this.send.coinControl.selected;
    this.go('send', { keep: true });
    this.send.step = 1;
    this.send.to = ''; this.send.amount = ''; this.send.err = '';
    this.send.preview = null; this.send.result = null;
    this.send.coinControl = { show: true, selected };
    this.$nextTick(() => document.getElementById('send-to')?.focus());
  },

  /** {txid, vout} pairs for the API call, or undefined for automatic
   *  selection (the request omits `inputs` entirely — the backend treats
   *  that differently from an explicit empty list). */
  coinControlInputs() {
    if (!this.coinControlActive()) return undefined;
    return Object.values(this.send.coinControl.selected)
      .map((u) => ({ txid: u.txid, vout: u.vout }));
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
          inputs: this.coinControlInputs(),
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
        ...(this.coinControlActive() ? [{ key: 'Spending',
          value: this.coinControlCount() + ' coin(s) you chose — '
               + fmtAmount(this.coinControlTotal(), { unit: true }) + ' total' }] : []),
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
          inputs: this.coinControlInputs(),
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

  /** Shared by the Send picker and the unstake destination picker. */
  filterAddressBook(query) {
    const q = (query || '').trim().toLowerCase();
    let list = (this.book.addresses || []);
    if (q) {
      list = list.filter((a) =>
        (a.label || '').toLowerCase().includes(q) || a.address.toLowerCase().includes(q));
    }
    return list.slice(0, 40);
  },

  pickerResults() { return this.filterAddressBook(this.send.pickerQuery); },

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
