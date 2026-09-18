/* B3 Hive — batch actions (Advanced).

   Recipes are declarative JSON validated by backend/app/batch_engine.py. The
   engine enforces hard rails no recipe can weaken: plain P2PKH only (so B3S1
   stake carriers, B3A1 colored-asset envelopes and B3MC metadata cells are
   NEVER spent), Decimal 9dp money, fee clamped up to the relay floor, inputs
   capped at 675, and every broadcast gated by testmempoolaccept.

   The UI's job is to make a JSON recipe legible, which is what
   `recipeSummary()` does: it renders the recipe back as an English sentence
   so the operator can see what it will do without reading the object.

   Gating note: preview needs only a session (batch.py require_2fa), while
   execute needs an unlocked wallet. So the 423 passphrase prompt appears at
   Execute, not at Preview. */

import { fmtAmount, truncAddr, isValidAddress } from '../core/format.js';

const EMPTY_FORM = {
  name: '', sources: '', destination: '',
  sort: 'smallest', min_utxo_value: '',
  inputs_per_tx: '', max_batches: '', min_output: '',
  fee_mode: 'estimate', fee_rate: '',
};

export const batchMixin = {

  async loadRecipes() {
    this.batch.busy = true;
    try {
      const d = await this.api('/api/batch/recipes');
      this.batch.recipes = d.recipes || [];
      this.batch.loaded = true;
      this.batch.err = '';
    } catch (e) {
      this.batch.err = e.message;
      this.batch.loaded = true;
    }
    this.batch.busy = false;
  },

  /* ------------------------------------------------------------- editor -- */

  newRecipe() {
    this.batch.editing = true;
    this.batch.editId = null;
    this.batch.form = { ...EMPTY_FORM };
    this.batch.err = '';
    this.$nextTick(() => document.getElementById('recipe-name')?.focus());
  },

  editRecipe(r) {
    const rec = r.recipe || {};
    this.batch.editing = true;
    this.batch.editId = r.id;
    this.batch.preview = null;
    this.batch.results = null;
    this.batch.err = '';
    this.batch.form = {
      name: r.name || '',
      // Stored sources are {kind, value}; the editor works in plain lines.
      sources: (rec.filters?.sources || [])
        .map((s) => (typeof s === 'string' ? s
          : (s.kind === 'label' ? 'label:' + s.value : s.value)))
        .join('\n'),
      destination: rec.action?.destination || '',
      sort: rec.filters?.sort || 'smallest',
      min_utxo_value: rec.filters?.min_utxo_value ?? '',
      inputs_per_tx: rec.limits?.inputs_per_tx != null ? String(rec.limits.inputs_per_tx) : '',
      max_batches: rec.limits?.max_batches != null ? String(rec.limits.max_batches) : '',
      min_output: rec.limits?.min_output ?? '',
      fee_mode: rec.fees?.mode || 'estimate',
      fee_rate: rec.fees?.fee_rate ?? '',
    };
  },

  cancelEdit() {
    this.batch.editing = false;
    this.batch.editId = null;
    this.batch.err = '';
  },

  /** Form -> recipe JSON. Ported from the previous batchFormToRecipe(); the
   *  shape must match validate_recipe() in backend/app/batch_engine.py. */
  formToRecipe() {
    const f = this.batch.form;
    const sources = f.sources.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    const recipe = {
      name: f.name.trim(),
      filters: { sources, sort: f.sort || 'smallest' },
      action: { type: 'consolidate', destination: f.destination.trim() },
    };
    if (f.min_utxo_value !== '') recipe.filters.min_utxo_value = f.min_utxo_value;

    const limits = {};
    if (f.inputs_per_tx !== '') limits.inputs_per_tx = parseInt(f.inputs_per_tx, 10);
    if (f.max_batches !== '') limits.max_batches = parseInt(f.max_batches, 10);
    if (f.min_output !== '') limits.min_output = f.min_output;
    if (Object.keys(limits).length) recipe.limits = limits;

    const fees = { mode: f.fee_mode || 'estimate' };
    if (f.fee_rate !== '') fees.fee_rate = f.fee_rate;
    if (fees.mode === 'estimate') fees.fee_target = 6;
    recipe.fees = fees;

    return recipe;
  },

  /** Client-side checks that mirror the engine, so the user gets a plain
   *  message instead of a 422 round-trip. */
  recipeProblem() {
    const f = this.batch.form;
    if (!f.name.trim()) return 'Give the recipe a name.';
    if (f.name.trim().length > 64) return 'The name can be at most 64 characters.';
    const sources = f.sources.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    if (!sources.length) return 'Add at least one source address or label.';
    if (sources.length > 50) return 'At most 50 sources.';
    for (const s of sources) {
      if (s.toLowerCase().startsWith('label:')) {
        if (!s.slice(6).trim()) return 'One of the label: entries is empty.';
      } else if (!isValidAddress(s)) {
        return '“' + truncAddr(s, 10, 4) + '” is not a valid B3 address.';
      }
    }
    if (!isValidAddress(f.destination.trim())) return 'The destination is not a valid B3 address.';
    const ipt = f.inputs_per_tx === '' ? 50 : parseInt(f.inputs_per_tx, 10);
    if (!(ipt >= 1 && ipt <= 675)) return 'Inputs per transaction must be between 1 and 675.';
    const mb = f.max_batches === '' ? 5 : parseInt(f.max_batches, 10);
    if (!(mb >= 1 && mb <= 25)) return 'Max transactions must be between 1 and 25.';
    return null;
  },

  async saveRecipe() {
    const problem = this.recipeProblem();
    if (problem) { this.batch.err = problem; return; }
    this.batch.err = '';
    this.batch.working = true;
    try {
      const recipe = this.formToRecipe();
      if (this.batch.editId != null) {
        await this.api('/api/batch/recipes/' + this.batch.editId, {
          method: 'PUT', body: JSON.stringify(recipe),
        });
        this.showToast('Recipe updated');
      } else {
        await this.api('/api/batch/recipes', {
          method: 'POST', body: JSON.stringify(recipe),
        });
        this.showToast('Recipe saved');
      }
      this.batch.editing = false;
      this.batch.editId = null;
      this.batch.form = { ...EMPTY_FORM };
      this.batch.preview = null;
      await this.loadRecipes();
    } catch (e) {
      this.batch.err = e.message;
    }
    this.batch.working = false;
  },

  /** Modal confirmation — window.confirm is banned for anything that touches
   *  a wallet, and deleting a recipe is destructive enough to deserve it. */
  deleteRecipe(r) {
    this.confirm({
      title: 'Delete “' + r.name + '”?',
      body: 'The recipe is removed. No coins move and nothing already broadcast is '
          + 'affected — you would just have to set the recipe up again.',
      confirmLabel: 'Delete recipe',
      danger: true,
      run: async () => {
        this.batch.working = true;
        try {
          await this.api('/api/batch/recipes/' + r.id, { method: 'DELETE' });
          if (this.batch.editId === r.id) this.cancelEdit();
          if (this.batch.previewFor === r.id) { this.batch.preview = null; this.batch.previewFor = null; }
          this.showToast('Recipe deleted');
          await this.loadRecipes();
        } catch (e) { this.reportError(e); }
        this.batch.working = false;
      },
    });
  },

  /* ------------------------------------------------- preview and execute -- */

  async previewRecipe(r) {
    this.batch.err = '';
    this.batch.working = true;
    this.batch.preview = null;
    this.batch.results = null;
    this.batch.previewFor = r.id;
    try {
      this.batch.preview = await this.api('/api/batch/recipes/' + r.id + '/preview', {
        method: 'POST',
      });
    } catch (e) {
      if (!e.cancelled) this.batch.err = e.message;
      this.batch.previewFor = null;
    }
    this.batch.working = false;
  },

  clearPreview() {
    this.batch.preview = null;
    this.batch.previewFor = null;
    this.batch.results = null;
  },

  liveBatches() {
    return (this.batch.preview?.batches || []).filter((b) => !b.below_min_output);
  },

  executeRecipe() {
    const p = this.batch.preview;
    const id = this.batch.previewFor;
    if (!p || id == null) return;
    const n = this.liveBatches().length;
    if (!n) {
      this.showToast('Nothing to do — every batch is below the minimum output', 'warning');
      return;
    }
    this.confirm({
      title: 'Run “' + (p.recipe_name || 'this recipe') + '”?',
      body: 'This broadcasts ' + n + (n === 1 ? ' transaction' : ' transactions')
          + ' spending ' + p.eligible_utxos + ' of your outputs. Fees are paid now and '
          + 'broadcasting cannot be undone. Stake carriers and colored-asset envelopes '
          + 'are never included.',
      detail: [
        { key: 'Transactions', value: String(n) },
        { key: 'Outputs spent', value: String(p.eligible_utxos) },
        { key: 'Total fee', value: fmtAmount(p.total_fee, { unit: true }) },
        { key: 'Total output', value: fmtAmount(p.total_output, { unit: true }) },
        { key: 'Destination', value: p.destination, mono: true },
      ],
      confirmLabel: 'Run now',
      danger: true,
      run: async () => {
        this.batch.working = true;
        try {
          const r = await this.api('/api/batch/recipes/' + id + '/execute', {
            method: 'POST',
            body: JSON.stringify({ confirm_token: p.confirm_token }),
          });
          this.batch.results = r.results || [];
          this.batch.preview = null;
          this.showToast('Batch complete');
          await Promise.allSettled([this.loadBalances(), this.loadHistory()]);
        } catch (e) {
          if (!e.cancelled) { this.batch.err = e.message; this.reportError(e); }
        }
        this.batch.working = false;
      },
    });
  },

  /* ------------------------------------------------------- legibility ----- */

  /** Render a recipe back as an English sentence. A declarative JSON object
   *  is precise but unreadable; this is what makes Batch usable by someone
   *  who did not write the recipe. */
  recipeSummary(r) {
    const rec = r.recipe || {};
    // GET /api/batch/recipes returns the raw submitted body, where sources are
    // plain strings; the engine's normalised form uses {kind, value}. Both
    // shapes reach this function, so normalise before counting.
    const srcs = (rec.filters?.sources || []).map((s) => (
      typeof s === 'string'
        ? (s.toLowerCase().startsWith('label:') ? 'label' : 'address')
        : (s.kind === 'label' ? 'label' : 'address')
    ));
    const labels = srcs.filter((k) => k === 'label').length;
    const addrs = srcs.length - labels;
    const parts = [];

    const sortWord = { smallest: 'smallest', largest: 'largest', oldest: 'oldest' }[
      rec.filters?.sort || 'smallest'];
    parts.push('Merge the ' + sortWord + ' outputs');

    const from = [];
    if (addrs) from.push(addrs + (addrs === 1 ? ' address' : ' addresses'));
    if (labels) from.push(labels + (labels === 1 ? ' label' : ' labels'));
    if (from.length) parts.push('from ' + from.join(' and '));

    const min = rec.filters?.min_utxo_value;
    if (min && Number(min) > 0) {
      parts.push('worth at least ' + fmtAmount(min, { unit: true, maxDecimals: 4 }));
    }

    parts.push('into ' + truncAddr(rec.action?.destination || '', 8, 6));

    const ipt = rec.limits?.inputs_per_tx ?? 50;
    const mb = rec.limits?.max_batches ?? 5;
    parts.push('in up to ' + mb + (mb === 1 ? ' transaction' : ' transactions')
      + ' of ' + ipt + ' inputs');

    parts.push(rec.fees?.mode === 'fixed'
      ? 'at a fixed fee rate of ' + fmtAmount(rec.fees.fee_rate, { maxDecimals: 9 }) + ' B3/kB'
      : 'at the fee rate your node estimates');

    return parts.join(' ') + '.';
  },

  /** Live version of the same sentence, for the editor. */
  draftSummary() {
    if (this.recipeProblem()) return null;
    return this.recipeSummary({ name: this.batch.form.name, recipe: this.formToRecipe() });
  },
};
