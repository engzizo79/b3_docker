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

  /* ------------------------------------------- guided start (the pipeline) */

  /* Earning blocks needs THREE things, in order (verified against the
     node source): (1) locked STAKE weight (createstake), (2) an on-chain
     finality key binding (bindfinalitykey) - without it the validator is
     NOT block-eligible, (3) the running staking loop (startstaking).
     The old button did only (3) and silently earned nothing. */
  async startStaking() {
    const blocker = this.stakingBlocker();
    if (blocker) { this.showToast(blocker, 'warning'); return; }
    this.staking.working = true;
    try {
      await this.loadValidator();
      const v = this.staking.validator;
      if (v && v.missing && v.missing.length) {
        this.openStartFlow();
      } else {
        await this.api('/api/wallet/staking/start', { method: 'POST' });
        await this.loadStakingSnapshot();
        this.showToast('Staking started — your coins are now working');
      }
    } catch (e) { this.reportError(e); }
    this.staking.working = false;
  },

  async loadValidator() {
    try {
      this.staking.validator = await this.api('/api/staking/validator');
    } catch { this.staking.validator = null; }
  },

  /* Locking MORE coins alongside existing stakes: the same createstake RPC,
 but without touching the already-done bind/start steps, so the loop keeps
 running and no unstake is needed first. */
 openAddStake() {
 const spendable = Number(this.spendableAmount()) || 0;
 const v = this.staking.validator || {};
 const min = v.min_stake ? Number(v.min_stake) : null;
 let suggest = spendable > 0 ? spendable : (min || 0);
 if (min && suggest < min) suggest = min;
 this.addStake = { show: true, amount: String(suggest), err: '', busy: false };
 this.$nextTick(() => document.getElementById('addstake-amount').focus());
 },

 async submitAddStake() {
 const f = this.addStake;
 if (!f || f.busy) return;
 const amt = Number(f.amount);
 if (!f.amount || !Number.isFinite(amt) || amt <= 0) {
 f.err = 'Enter an amount to lock'; return;
 }
 const spendable = Number(this.spendableAmount()) || 0;
 const reserve = 0.001; // fee floor: never lock literally everything
 if (amt > spendable - reserve) {
 f.err = 'Leave a small amount for the stake transaction fee (try '
 + fmtAmount(Math.max(0, spendable - reserve), { maxDecimals: 3 }) + ' B3)';
 return;
 }
 f.busy = true; f.err = '';
 try {
 await this.api('/api/staking/stake', {
 method: 'POST',
 body: JSON.stringify({ amount: f.amount }),
 });
 this.showToast('Coins locked — the new stake is on its way to active');
 f.show = false;
 await Promise.allSettled([this.loadStakingSnapshot(), this.loadValidator(), this.loadBalances()]);
 } catch (e) {
 if (f.show) f.err = (e && e.message) ? e.message : 'Could not lock the coins. Try again.';
 else this.reportError(e);
 }
 f.busy = false;
 },

 openStartFlow() {
    const v = this.staking.validator || {};
    const min = v.min_stake ? Number(v.min_stake) : null;
    const spendable = Number(this.spendableAmount()) || 0;
    // Suggest: everything spendable if the whole balance can stake, else the
    // network minimum. Never suggest more than the user has.
    let suggest = spendable > 0 ? spendable : (min || 0);
    if (min && suggest < min) suggest = min;
    this.startFlow = {
      show: true, amount: String(suggest), err: '', busy: false, confirming: false,
    };
    this.$nextTick(() => document.getElementById('startflow-amount')?.focus());
  },

  startFlowAmountCheck() {
    const v = this.staking.validator || {};
    const min = v.min_stake ? Number(v.min_stake) : null;
    const amt = Number(this.startFlow.amount);
    if (!this.startFlow.amount || !Number.isFinite(amt) || amt <= 0) {
      return { ok: false, msg: 'Enter an amount to lock' };
    }
    if (min && amt < min) {
      return { ok: false, msg: 'The network minimum stake is ' + min + ' B3' };
    }
    const spendable = Number(this.spendableAmount());
    if (Number.isFinite(spendable) && amt > spendable) {
      return { ok: false, msg: 'You only have ' + this.fmtAmount(this.spendableAmount(), { unit: true }) + ' available' };
    }
    // Staking 100% of the spendable balance fails on chain because the
    // stake transaction needs a fee. Reserve a small dust amount so the
    // node can fund it. 0.001 B3 is a safe floor above any relay minimum.
    if (Number.isFinite(spendable) && amt >= spendable && spendable > 0.001) {
      return { ok: false, msg: 'Leave a small amount for the stake transaction fee (try ' + this.fmtAmount(String(Math.max(0, spendable - 0.001)), { unit: true }) + ')' };
    }
    return { ok: true, msg: '' };
  },

  startFlowAmountValid() { return this.startFlowAmountCheck().ok; },

  /** Performs the NEXT missing pipeline step. Called by the modal's
      primary button; re-checks /validator after each step so the
      checklist stays honest even if a step failed silently. */
  async runStartFlowStep() {
    const f = this.startFlow;
    if (!f || f.busy) return;
    await this.loadValidator();
    const v = this.staking.validator;
    if (!v) { f.err = 'Could not read staking status. Try again.'; return; }
    if (!v.missing || !v.missing.length) {
      f.show = false;
      this.showToast('Staking is fully set up');
      await this.loadStakingSnapshot();
      return;
    }
    const step = v.missing[0];
    f.busy = true; f.err = '';
    try {
      if (step === 'stake') {
        const c = this.startFlowAmountCheck();
        if (!c.ok) { f.err = c.msg; f.busy = false; return; }
        await this.api('/api/staking/stake', {
          method: 'POST', body: JSON.stringify({ amount: this.startFlow.amount }) });
        this.showToast('Coins locked — the stake is on its way to active');
      } else if (step === 'bind') {
        await this.api('/api/staking/finality/bind', { method: 'POST' });
        this.showToast('Finality key submitted — confirming on-chain');
        f.confirming = true;
        try {
          for (let i = 0; i < 20; i++) {
            await new Promise((r) => setTimeout(r, 6000));
            await this.loadValidator();
            const pv = this.staking.validator;
            if (pv && (!pv.missing || !pv.missing.includes('bind'))) break;
          }
        } finally { f.confirming = false; }
        const bv = this.staking.validator;
        if (bv && bv.missing && bv.missing.includes('bind')) {
          f.err = 'Binding not confirmed yet — it takes effect at the next epoch boundary. Close and reopen in a minute.';
        }
      } else { // start
        await this.api('/api/wallet/staking/start', { method: 'POST' });
        this.showToast('Staking loop started');
      }
      await this.loadValidator();
      const nv = this.staking.validator;
      if (nv && (!nv.missing || !nv.missing.length)) {
        f.show = false;
        this.showToast('Staking is fully set up — you are earning', 'success');
      }
    } catch (e) { f.err = (e && e.message) || 'That step failed. Try again.'; }
    f.busy = false;
    await this.loadStakingSnapshot();
  },

  /* Checklist helpers for the start-flow modal. */
  startFlowStepDone(step) {
    const v = this.staking.validator;
    if (!v || !v.missing) return false;
    return !v.missing.includes(step);
  },

  startFlowStepLabel(step) {
    if (this.startFlowStepDone(step)) return 'Done';
    if (step === 'stake') return 'Locks coins as stake weight';
    if (step === 'bind') return 'Makes you block-eligible';
    return 'Runs the block production';
  },

  startFlowNextLabel() {
    const v = this.staking.validator;
    if (!v || !v.missing || !v.missing.length) return 'Done';
    const step = v.missing[0];
    if (step === 'stake') return 'Lock ' + (this.startFlow.amount || '') + ' B3';
    if (step === 'bind') return 'Register finality key';
    return 'Turn on staking';
  },

  /* ------------------------------------------- leaving the validator set */

  /* The developer-documented procedure for operators going offline
     long-term: revokefinalitykey. NEVER automatic, needs an explicit
     acknowledgement, and is not a recovery step. */
  revokeFinality() {
    this.confirm({
      title: 'Revoke your finality key?',
      body: 'This is only for leaving the validator set and going offline for a '
      + 'long time. It broadcasts a revocation that removes your eligibility to '
      + 'produce staking blocks once the updated committee takes effect. It is '
      + 'not a recovery step. It does NOT unstake your coins and does not erase '
      + 'earlier signatures. Keep some spendable B3 for the transaction fee. If '
      + 'you are still signing, keep running until your validator has left the '
      + 'active committee.',
      detail: [
        { key: 'What happens', value: 'You stop being block-eligible' },
        { key: 'Your staked coins', value: 'Stay locked — unstake separately anytime' },
        { key: 'Takes effect', value: 'At the next epoch boundary' },
        { key: 'Reversible', value: 'Only by binding a new key later' },
      ],
      confirmLabel: 'I understand — revoke',
      danger: true,
      run: async () => {
        this.staking.working = true;
        try {
          const r = await this.api('/api/staking/finality/revoke', {
            method: 'POST', body: JSON.stringify({ ack: true }) });
          this.showToast('Revocation submitted — confirm it on the Staking page');
          await this.loadValidator();
          await this.loadStakingSnapshot();
        } catch (e) { this.reportError(e); }
        this.staking.working = false;
      },
    });
  },

  closeStartFlow() {
    this.startFlow = { show: false, amount: '', err: '', busy: false, confirming: false };
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
          await this.loadValidator();
          this.showToast('Staking stopped');
          const v = this.staking.validator;
          if (v && v.bound && !v.revoked) {
            this.$nextTick(() => {
              this.confirm({
                title: 'Also revoke your finality key?',
                body: 'You have stopped staking but your finality key is still '
                  + 'bound. The B3Hive developers recommend revoking it if you '
                  + 'intend to go offline long-term — it removes your block '
                  + 'eligibility once the committee handover completes. It does '
                  + 'NOT unstake your coins. If you plan to restart staking soon, '
                  + 'keep it bound and skip this step.',
                confirmLabel: 'Revoke finality key',
                danger: true,
                run: async () => { await this.revokeFinality(); },
              });
            });
          }
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
