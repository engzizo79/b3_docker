/* B3 Hive — staking, unstaking, and the two automations.

   Surfaces (brief §9.3):
     S1 autostake on restart      /api/staking/settings
     S2 scheduled consolidation   /api/staking/consolidation/*
     S3 one-click unstake         /api/staking/unstake      (two-phase)
     S4 human staking cards       /api/chain/staking
     S5 passphrase vault          /api/staking/settings + /vault/revoke

   Note an asymmetry worth designing around: consolidation PREVIEW requires an
   unlocked wallet (staking.py require_wallet_unlocked), whereas batch preview
   only needs a session. So consolidation asks for the passphrase one step
   earlier than Batch does — the UI says so up front instead of surprising
   the user with a modal. */

import { fmtAmount, isPositiveAmount, truncAddr, isValidAddress } from '../core/format.js';

export const stakingMixin = {

  /* -------------------------------------------------------------- status -- */

  /** Cheap poll for the Home snapshot. */
  async loadStakingSnapshot() {
    if (!this.walletStatus.reachable) return;
    try {
      const d = await this.api('/api/chain/staking');
      const info = d.staking || {};
      this.staking.info = info;
      const loop = info.staking || info;
      this.staking.active = !!(loop.running !== undefined ? loop.running : loop.staking);
      this.staking.available = loop.available !== false;
      this.staking.state = loop.state || null;
      this.staking.stakes = info.stakes || [];
      this.staking.weight = info.weight ?? loop.weight ?? null;
      this.staking.netWeight = info.netstakeweight ?? loop.netstakeweight ?? null;
    } catch {
      this.staking.info = null;
      this.staking.active = false;
      this.staking.stakes = [];
    }
  },

  async loadStaking() {
    this.staking.busy = true;
    await this.loadStakingSnapshot();
    this.staking.busy = false;
  },

  /** This wallet's share of total network stake weight, as a percentage. */
  weightShare() {
    const mine = Number(this.staking.weight);
    const net = Number(this.staking.netWeight);
    if (!Number.isFinite(mine) || !Number.isFinite(net) || net <= 0) return null;
    return Math.min(100, (mine / net) * 100);
  },

  /** Plain-language sentence for what the staker is doing right now. */
  stakingHeadline() {
    if (!this.staking.active) return 'Not staking yet';
    const s = this.staking.info?.staking || {};
    if (s.state && s.state !== 'staking') return 'Staking · ' + s.state;
    return 'Staking and earning';
  },

  stakingDetail() {
    const s = this.staking.info?.staking || {};
    const bits = [];
    if (s.blocks_produced != null) {
      bits.push(s.blocks_produced + (s.blocks_produced === 1 ? ' block produced' : ' blocks produced'));
    }
    if (s.finality_signing) bits.push('signing finality');
    if (s.last_signed_height != null && s.last_signed_height >= 0) {
      bits.push('last signed at ' + s.last_signed_height);
    }
    return bits.join(' · ');
  },

  /* ------------------------------------------------------- start and stop */

  async startStaking() {
    const blocker = this.stakingBlocker();
    if (blocker) { this.showToast(blocker, 'warning'); return; }
    this.staking.working = true;
    try {
      await this.api('/api/wallet/staking/start', { method: 'POST' });
      await this.loadStakingSnapshot();
      this.showToast('Staking started — your coins are now working');
    } catch (e) { this.reportError(e); }
    this.staking.working = false;
  },

  stopStaking() {
    this.confirm({
      title: 'Stop staking?',
      body: 'Your node stops producing blocks, so you stop earning rewards. Coins '
          + 'already locked in stakes stay staked until you unstake them separately. '
          + 'You can start again at any time.',
      confirmLabel: 'Stop staking',
      danger: true,
      run: async () => {
        this.staking.working = true;
        try {
          await this.api('/api/wallet/staking/stop', { method: 'POST' });
          await this.loadStakingSnapshot();
          this.showToast('Staking stopped');
        } catch (e) { this.reportError(e); }
        this.staking.working = false;
      },
    });
  },

  /* --------------------------------------------------- S3 one-click unstake */

  async previewUnstake(stake) {
    this.unstake = { target: stake, preview: null, busy: true };
    try {
      this.unstake.preview = await this.api('/api/staking/unstake', {
        method: 'POST',
        body: JSON.stringify({ txid: stake.txid, vout: stake.vout, confirm: false }),
      });
    } catch (e) {
      this.unstake = { target: null, preview: null, busy: false };
      this.reportError(e);
      return;
    }
    this.unstake.busy = false;
  },

  cancelUnstake() { this.unstake = { target: null, preview: null, busy: false }; },

  confirmUnstake() {
    const p = this.unstake.preview;
    if (!p) return;
    const amountText = fmtAmount(p.stake.amount, { unit: true });
    this.confirm({
      title: 'Unstake ' + amountText + '?',
      body: 'This moves the stake back into your spendable balance. It becomes '
          + 'spendable after one confirmation, and it stops earning rewards '
          + 'immediately. Broadcasting cannot be undone.'
          + (p.validator_warning
            ? ' This stake is currently ACTIVE — removing active stakes can affect '
            + 'network finality, so coordinate with the other validators if you run one.'
            : ''),
      detail: [
        { key: 'Amount', value: amountText },
        { key: 'Returns to', value: p.destination, mono: true },
        { key: 'Stake', value: truncAddr(p.stake.txid, 10, 4) + ':' + p.stake.vout, mono: true },
      ],
      confirmLabel: 'Unstake now',
      danger: true,
      run: async () => {
        this.unstake.busy = true;
        try {
          const r = await this.api('/api/staking/unstake', {
            method: 'POST',
            body: JSON.stringify({
              txid: this.unstake.target.txid,
              vout: this.unstake.target.vout,
              confirm: true,
              confirm_token: p.confirm_token,
            }),
          });
          this.showToast('Unstaked — ' + amountText + ' is on its way back');
          this.unstake = { target: null, preview: null, busy: false };
          await Promise.allSettled([this.loadStakingSnapshot(), this.loadBalances(), this.loadHistory()]);
          if (r.txid) this.lastTxid = r.txid;
        } catch (e) { this.reportError(e); }
        this.unstake.busy = false;
      },
    });
  },

  /* ------------------------------------------- S1/S5 autostake + vault --- */

  async loadStakingSettings() {
    try {
      this.autostake.settings = await this.api('/api/staking/settings');
      this.autostake.form = {
        enabled: !!this.autostake.settings.autostake_enabled,
        target: this.autostake.settings.autostake_target || '',
        reserve: this.autostake.settings.autostake_reserve || '',
      };
    } catch { this.autostake.settings = null; }
  },

  async saveAutostake() {
    const f = this.autostake.form;
    if (f.enabled && !this.autostake.settings?.vault_stored && !this.autostake.passphrase) {
      this.showToast('Enter your wallet passphrase so unattended top-ups can run', 'danger');
      return;
    }
    if (f.enabled && !this.autostake.ack) {
      this.showToast('Tick the risk acknowledgement to enable unattended staking', 'danger');
      return;
    }
    this.autostake.busy = true;
    try {
      this.autostake.settings = await this.api('/api/staking/settings', {
        method: 'POST',
        body: JSON.stringify({
          autostake_enabled: !!f.enabled,
          autostake_target: f.target || '0',
          autostake_reserve: f.reserve || '0',
          passphrase: this.autostake.passphrase || '',
          acknowledge_risk: !!this.autostake.ack,
        }),
      });
      this.autostake.passphrase = '';
      this.showToast('Staking automation saved');
    } catch (e) { this.reportError(e); }
    this.autostake.busy = false;
  },

  revokeVault() {
    this.confirm({
      title: 'Remove the stored passphrase?',
      body: 'Unattended staking turns off and the encrypted passphrase is wiped from '
          + 'this machine. Staking you started by hand keeps running, but top-ups '
          + 'and scheduled sweeps will stop until you store it again.',
      confirmLabel: 'Remove passphrase',
      danger: true,
      run: async () => {
        try {
          await this.api('/api/staking/vault/revoke', { method: 'POST' });
          this.autostake.ack = false;
          await this.loadStakingSettings();
          this.showToast('Passphrase removed — unattended staking is off');
        } catch (e) { this.reportError(e); }
      },
    });
  },

  async runReconcile() {
    this.autostake.busy = true;
    try {
      const r = await this.api('/api/staking/reconcile', { method: 'POST' });
      if (r.ran) {
        this.showToast(isPositiveAmount(r.topped_up)
          ? 'Topped up ' + fmtAmount(r.topped_up, { unit: true })
          : 'Checked — nothing needed topping up');
      } else {
        this.showToast(this.reconcileReason(r.reason), 'warning');
      }
      await Promise.allSettled([this.loadStakingSnapshot(), this.loadBalances()]);
    } catch (e) { this.reportError(e); }
    this.autostake.busy = false;
  },

  reconcileReason(reason) {
    const r = String(reason || '').toLowerCase();
    if (r.includes('disabled')) return 'Unattended staking is switched off.';
    if (r.includes('passphrase') || r.includes('vault')) return 'No stored passphrase — save one first.';
    if (r.includes('no wallet')) return 'No wallet is loaded.';
    if (r.includes('unreachable')) return 'Your node is not responding yet.';
    return 'Nothing to do right now.';
  },

  /* ------------------------------------------- S2 consolidation sweep ---- */

  async loadConsolidation() {
    try {
      const c = await this.api('/api/staking/consolidation/settings');
      this.cons.settings = c;
      this.cons.form = {
        enabled: !!c.enabled,
        destination: c.destination || '',
        interval_minutes: c.interval_minutes ?? 1440,
        min_utxo_value: c.min_utxo_value ?? '0',
        inputs_per_tx: c.inputs_per_tx ?? 50,
        max_batches: c.max_batches ?? 5,
        min_output: c.min_output ?? '0.0001',
        fee_mode: c.fee_mode || 'estimate',
        fee_rate: c.fee_rate || '0.0001',
        restake_after: !!c.restake_after,
      };
    } catch { this.cons.settings = null; }
  },

  consDestValid() {
    const d = (this.cons.form.destination || '').trim();
    return !d || isValidAddress(d);
  },

  async saveConsolidation() {
    if (!this.consDestValid()) {
      this.showToast('That destination is not a valid B3 address', 'danger'); return;
    }
    this.cons.busy = true;
    try {
      this.cons.settings = await this.api('/api/staking/consolidation/settings', {
        method: 'POST',
        body: JSON.stringify(this.cons.form),
      });
      this.showToast('Consolidation settings saved');
    } catch (e) { this.reportError(e); }
    this.cons.busy = false;
  },

  async previewConsolidation() {
    if (!this.cons.form?.destination) {
      this.showToast('Set a destination address first', 'warning');
      this.go('automation');
      return;
    }
    this.cons.busy = true; this.cons.preview = null;
    try {
      this.cons.preview = await this.api('/api/staking/consolidation/preview', { method: 'POST' });
    } catch (e) {
      if (!e.cancelled) this.reportError(e);
    }
    this.cons.busy = false;
  },

  cancelConsolidation() { this.cons.preview = null; },

  /** Batches that will actually broadcast (the rest are below min_output). */
  consLiveBatches() {
    return (this.cons.preview?.batches || []).filter((b) => !b.below_min_output);
  },

  executeConsolidation() {
    const p = this.cons.preview;
    if (!p) return;
    const n = this.consLiveBatches().length;
    this.confirm({
      title: 'Consolidate ' + p.eligible_utxos + ' outputs?',
      body: 'This broadcasts ' + n + (n === 1 ? ' transaction' : ' transactions')
          + ' that merge your small outputs into one, to make future fees cheaper. '
          + 'The fee is paid now and cannot be undone.'
          + (p.restake_after ? ' The merged output is then staked immediately.' : ''),
      detail: [
        { key: 'Outputs merged', value: String(p.eligible_utxos) },
        { key: 'Total fee', value: fmtAmount(p.total_fee, { unit: true }) },
        { key: 'You receive', value: fmtAmount(p.total_output, { unit: true }) },
        { key: 'Into', value: p.destination, mono: true },
      ],
      confirmLabel: 'Consolidate now',
      danger: true,
      run: async () => {
        this.cons.working = true;
        try {
          const r = await this.api('/api/staking/consolidation/execute', {
            method: 'POST',
            body: JSON.stringify({ confirm_token: p.confirm_token }),
          });
          const done = (r.results || []).filter((x) => !x.skipped).length;
          this.showToast('Consolidated — ' + done + (done === 1 ? ' transaction' : ' transactions') + ' broadcast');
          this.cons.preview = null;
          this.cons.results = r.results || [];
          await Promise.allSettled([this.loadBalances(), this.loadHistory(), this.loadStakingSnapshot()]);
        } catch (e) { this.reportError(e); }
        this.cons.working = false;
      },
    });
  },
};
