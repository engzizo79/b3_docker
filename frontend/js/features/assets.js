/* B3 Hive — Assets: FN Coin, colored assets, FlowMesh markets and validator.

   This view is ALWAYS available, not hidden when the wallet holds nothing.
   FN Coin is a real part of this chain, so an empty Assets view teaches what
   it is and what creating one costs, rather than showing a dead table.

   FN CREATION FACTS, read from the node source rather than guessed:
     src/wallet/rpc/assets.cpp:1114  createfncoin [address] [options]
     src/modern/fn_pod.h:147         RequiredFnPodDisintegration()
     src/consensus/amount.h:20       1 B3 = KILO_COIN = 1e9 base units

   - createfncoin mints EXACTLY ONE FN Coin and permanently DESTROYS a
     consensus-pinned amount of native B3 ("proof of disintegration"). The B3
     is not spent to anyone — it ceases to exist. That must be unmissable.
   - The price is tiered by how many have been created before yours:
       slots    0- 499  ->  15,000 B3   (tier 1)
       slots  500- 999  ->  30,000 B3   (tier 2)
       slots 1000+      ->  60,000 B3   (tier 3)
   - Only ONE creation may be in flight network-wide per slot. A second
     attempt fails with "A modern FN creation for the current slot is already
     pending", so the UI pre-empts that rather than reporting it afterwards.
   - It requires an unlocked wallet (HELP_REQUIRING_PASSPHRASE).

   BACKEND GAP (documented, not faked): `createfncoin` is not on the RPC
   allowlist (backend/app/rpc.py) and no endpoint proxies it, and /api/assets
   whitelists only six `fn` fields, dropping tier / next_slot /
   required_disintegration. Since backend/ is read-only for this redesign, the
   cost is computed here from the consensus table above using the
   `modern_issued` counter that IS exposed, and the final action degrades to
   the exact b3coin-cli command instead of a dead button. `fnCreateSupported`
   probes for the endpoint so this lights up automatically if it is added. */

import { fmtAmount, fmtInt, truncAddr } from '../core/format.js';

const FN_TIERS = [
  { upTo: 500,      cost: '15000.000000000', tier: 1 },
  { upTo: 1000,     cost: '30000.000000000', tier: 2 },
  { upTo: Infinity, cost: '60000.000000000', tier: 3 },
];

export const assetsMixin = {

  async loadAssets() {
    this.assets.busy = true;
    try {
      const d = await this.api('/api/assets');
      this.assets.list = d.assets || [];
      this.assets.fn = d.fn || null;
      this.assets.loaded = true;
    } catch {
      this.assets.list = [];
      this.assets.fn = null;
      this.assets.loaded = true;
    }
    try {
      const d = await this.api('/api/assets/markets');
      this.assets.markets = d.markets || [];
    } catch { this.assets.markets = []; }

    // NOTE: never probe /api/assets/fn/create (apiSupports POSTs, which would
    // actually create an FN Coin and destroy B3). The endpoint always exists.
    this.assets.busy = false;
  },

  /* ------------------------------------------------------- presentation -- */

  /** Asset amounts are exact integer base units in the asset's own decimals
   *  — FN has 0, bUSD has 6. Never reuse the 9-dp B3 formatter here. */
  fmtAssetAmount(asset, raw) {
    const dp = Number(asset?.decimals ?? 0);
    const v = String(raw ?? '0');
    if (!dp) return fmtInt(v);
    const s = v.padStart(dp + 1, '0');
    const int = s.slice(0, -dp).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    const frac = s.slice(-dp).replace(/0+$/, '');
    return frac ? int + '.' + frac : int;
  },

  assetTicker(a) {
    return a.ticker || (a.asset_id ? a.asset_id.slice(0, 6).toUpperCase() : 'ASSET');
  },

  assetKindLabel(kind) {
    const map = { fn: 'FN Coin', colored: 'Colored asset', bridge: 'Bridged' };
    return map[kind] || (kind || 'Asset');
  },

  hasAssets() { return (this.assets.list || []).length > 0; },

  /* ------------------------------------------------------ FN economics --- */

  /** Which tier the NEXT creation falls into, and what it destroys. */
  fnNextSlot() {
    const n = this.assets.fn?.modern_issued;
    return Number.isFinite(Number(n)) ? Number(n) : null;
  },

  fnTier() {
    const slot = this.fnNextSlot();
    if (slot === null) return null;
    const hit = FN_TIERS.find((t) => slot < t.upTo);
    return { ...hit, slot };
  },

  fnCost() {
    const t = this.fnTier();
    return t ? t.cost : null;
  },

  fnTiers() {
    const cur = this.fnTier();
    return FN_TIERS.map((t, i) => ({
      tier: t.tier,
      cost: t.cost,
      range: i === 0 ? 'first 500' : (i === 1 ? 'next 500' : 'after that'),
      now: !!cur && cur.tier === t.tier,
    }));
  },

  fnRemaining() {
    const fn = this.assets.fn || {};
    const cap = Number(fn.modern_capacity);
    const made = Number(fn.modern_issued);
    if (!Number.isFinite(cap) || !Number.isFinite(made)) return null;
    return Math.max(0, cap - made);
  },

  /** Every reason FN creation might not be possible right now, in the order
   *  the node itself checks them — so the user learns the real blocker
   *  instead of discovering it after an attempt. */
  fnBlocker() {
    const fn = this.assets.fn;
    if (!fn) return 'Asset information is not available yet.';
    if (!fn.configured) return 'FN Coin is not configured on this chain.';
    if (!fn.pod_active) return 'Proof-free FN creation is not active on this chain yet.';
    if (!fn.counter_known) return 'Your node is still syncing the FN counter — wait until it is caught up.';
    if (this.fnRemaining() === 0) return 'The lifetime FN Coin limit has been reached. No more can ever be created.';
    if (this.walletState() === 'no-wallet') return 'You need a wallet loaded first.';
    if (!this.synced()) return 'Wait until your node is fully synced — creation depends on the current block.';
    const cost = this.fnCost();
    if (cost && this.wallet.balance != null
        && this.compareAmounts(this.wallet.balance, cost) < 0) {
      return 'You need ' + fmtAmount(cost, { unit: true, maxDecimals: 0 })
           + ' of confirmed B3 to destroy, and you have '
           + fmtAmount(this.wallet.balance, { unit: true, maxDecimals: 2 }) + '.';
    }
    return null;
  },

  /** The exact CLI equivalent, offered while no backend endpoint exists so
   *  the user still has a way forward. */
  fnCliCommand() {
    return 'b3coin-cli -rpcwallet=' + (this.walletStatus.loaded[0] || '') + ' createfncoin';
  },

  openFnCreate() {
    this.fnCreate.open = true;
    this.fnCreate.ack = false;
    this.fnCreate.address = '';
  },

  closeFnCreate() { this.fnCreate.open = false; },

  /** Guarded creation. Only reachable when the endpoint actually exists. */
  createFnCoin() {
    const blocker = this.fnBlocker();
    if (blocker) { this.showToast(blocker, 'warning'); return; }
    if (!this.fnCreate.ack) {
      this.showToast('Confirm you understand the B3 is destroyed, not transferred', 'danger');
      return;
    }
    const cost = this.fnCost();
    const costText = fmtAmount(cost, { unit: true, maxDecimals: 0 });
    this.confirm({
      title: 'Destroy ' + costText + ' to create 1 FN Coin?',
      body: 'This permanently destroys ' + costText + ' from your wallet. The coins are '
          + 'not sent to anyone and not held in escrow — they stop existing, and no one '
          + 'can ever return them. In exchange you receive exactly one FN Coin. '
          + 'This cannot be undone.',
      detail: [
        { key: 'B3 destroyed', value: costText },
        { key: 'You receive', value: '1 FN Coin' },
        { key: 'Price tier', value: 'Tier ' + this.fnTier().tier + ' (slot ' + this.fnNextSlot() + ')' },
        ...(this.fnCreate.address
          ? [{ key: 'Owner address', value: this.fnCreate.address, mono: true }] : []),
      ],
      confirmLabel: 'Destroy B3 and create',
      danger: true,
      run: async () => {
        this.fnCreate.busy = true;
        try {
          const body = {};
          if (this.fnCreate.address) body.address = this.fnCreate.address.trim();
          const r = await this.api('/api/assets/fn/create', {
            method: 'POST',
            body: JSON.stringify(body),
          });
          this.fnCreate.open = false;
          this.fnCreate.result = r;
          this.showToast('FN Coin created');
          await Promise.allSettled([this.loadAssets(), this.loadBalances(), this.loadHistory()]);
        } catch (e) { this.reportError(e); }
        this.fnCreate.busy = false;
      },
    });
  },

  /* ----------------------------------------------------- FlowMesh markets */

  marketLabel(m) {
    return truncAddr(m.market_id || '', 10, 4);
  },

  /* --------------------------------------------------- FlowMesh validator */

  validatorAction(action) {
    const starting = action === 'start';
    this.confirm({
      title: starting ? 'Start the FlowMesh validator?' : 'Stop the FlowMesh validator?',
      body: starting
        ? 'Your node will take on validator duties for FlowMesh markets. This is a '
        + 'responsibility: validators are expected to stay online, and your wallet '
        + 'must stay unlocked for signing.'
        : 'Your node stops validating FlowMesh markets. If too many validators leave '
        + 'at once, market finality and the bridge can stall — check with the other '
        + 'operators before stopping.',
      confirmLabel: starting ? 'Start validator' : 'Stop validator',
      danger: !starting,
      run: async () => {
        this.assets.validatorBusy = true;
        try {
          await this.api('/api/assets/validator/' + action, { method: 'POST' });
          this.showToast(starting ? 'FlowMesh validator started' : 'FlowMesh validator stopped');
          await this.loadAssets();
        } catch (e) { this.reportError(e); }
        this.assets.validatorBusy = false;
      },
    });
  },
};
