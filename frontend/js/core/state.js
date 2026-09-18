/* B3 Hive — derived state.

   Two jobs:

   1. THREE-STATE WALLET. The old header badge rendered "Locked" from
      `wallet.loaded && !wallet_unlocked`, which conflated "no wallet loaded"
      with "wallet locked". `walletState()` is now the single source every
      consumer reads, built from /api/setup/wallet/status (reachable +
      loaded[]) and /api/auth/status (wallet_unlocked).

   2. THE GUIDANCE ENGINE. `nextStep()` walks a priority-ordered rule list and
      returns exactly ONE thing for the user to do. This is what stops the app
      feeling like a dev dashboard: there is always one clear next step, and
      when there is genuinely nothing to do the card collapses to a single
      line so a healthy wallet shows no dead whitespace.

   Sync/bootstrap progress deliberately lives in a separate always-visible
   banner (see syncBanner()), because the brief requires it on EVERY view,
   not just Home. */

import { isPositiveAmount, fmtInt, fmtAmount, fmtDuration } from './format.js';

const LOCALHOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1', '']);
const BACKUP_KEY = 'b3-last-backup';
const BACKUP_NAG_DAYS = 30;

export const stateMixin = {

  /* ------------------------------------------------------------- wallet -- */

  /** 'no-node' | 'no-wallet' | 'locked' | 'unlocked' */
  walletState() {
    if (!this.walletStatus.checked) return 'no-node';
    if (!this.walletStatus.reachable) return 'no-node';
    if (!this.walletStatus.loaded.length) return 'no-wallet';
    return this.session.wallet_unlocked ? 'unlocked' : 'locked';
  },

  walletLoaded() { return this.walletState() === 'locked' || this.walletState() === 'unlocked'; },
  walletUnlocked() { return this.walletState() === 'unlocked'; },

  walletStateLabel() {
    switch (this.walletState()) {
      case 'unlocked': return this.unlockLeft > 0
        ? 'Unlocked · ' + this.fmtClock(this.unlockLeft) : 'Unlocked';
      case 'locked':    return 'Locked';
      case 'no-wallet': return 'No wallet';
      default:          return this.nodeDown() ? 'Node offline' : 'Connecting…';
    }
  },

  walletStateIcon() {
    switch (this.walletState()) {
      case 'unlocked': return 'unlock';
      case 'locked':   return 'lock';
      case 'no-wallet':return 'wallet';
      default:         return 'alert';
    }
  },

  walletStateHint() {
    switch (this.walletState()) {
      case 'unlocked': return 'Your wallet is unlocked and can sign transactions. It re-locks automatically.';
      case 'locked':   return 'Your wallet is loaded but locked. You will be asked for the passphrase when you need it.';
      case 'no-wallet':return 'No wallet is loaded. Create or load one to see a balance.';
      default:         return 'Your node is not responding yet.';
    }
  },

  activeWalletName() {
    const w = this.walletStatus.loaded[0];
    if (w === undefined) return null;
    return w === '' ? 'Default wallet' : w;
  },

  /* --------------------------------------------------------------- node -- */

  nodeDown() { return this.chain.blocks === null; },

  /** Honest sync progress: local blocks against the explorer tip, never raw
   *  verificationprogress (which reports ~100% at 30k/820k on this chain).
   *  The backend already computes this in chain.py; we only present it. */
  syncPercent() {
    const s = this.chain.sync;
    if (!s || s.percent == null) return null;
    return Math.min(100, s.percent);
  },

  synced() {
    const s = this.chain.sync;
    return !!s && s.behind != null && s.behind === 0;
  },

  /** Rough catch-up estimate. B3 targets ~1-minute blocks. */
  syncEta() {
    const s = this.chain.sync;
    if (!s || !s.behind) return null;
    return fmtDuration(s.behind * 60);
  },

  bootstrapPhase() {
    if (this.wizard.progress && this.wizard.progress.phase) return this.wizard.progress.phase;
    const b = this.setup.bootstrap;
    return (b && b.phase) || 'idle';
  },

  bootstrapActive() {
    return ['downloading', 'verifying', 'extracting', 'wiping'].includes(this.bootstrapPhase());
  },

  bootstrapPct() {
    const p = this.wizard.progress || {};
    if (p.phase !== 'downloading' || !p.size) return 0;
    return Math.min(100, Math.round(((p.bytes || 0) / p.size) * 100));
  },

  /** The always-visible sync/bootstrap banner (brief: every view, not just
   *  Home). Suppressed while the wizard owns the screen. */
  syncBanner() {
    if (this.setupActive()) return null;
    if (!this.setup.wizard_done) return null;

    const phase = this.bootstrapPhase();
    if (this.bootstrapActive()) {
      const labels = {
        downloading: 'Downloading the chain snapshot',
        verifying:   'Checking the snapshot is genuine',
        extracting:  'Unpacking the snapshot',
        wiping:      'Clearing the old chain data',
      };
      return {
        kind: 'bootstrap',
        title: labels[phase] || 'Preparing your node',
        detail: phase === 'downloading' ? this.bootstrapMb() : 'This can take a few minutes.',
        pct: phase === 'downloading' ? this.bootstrapPct() : null,
      };
    }
    if (this.nodeDown()) {
      return {
        kind: 'starting',
        title: 'Your node is starting',
        detail: 'B3 nodes take a few minutes to scan their index before they answer.',
        pct: null,
      };
    }
    const s = this.chain.sync;
    if (s && s.behind > 0) {
      const eta = this.syncEta();
      return {
        kind: 'sync',
        title: 'Catching up with the network',
        detail: fmtInt(s.blocks) + ' of ' + fmtInt(s.tip || s.headers) + ' blocks'
                + (eta ? ' · about ' + eta + ' behind' : ''),
        pct: this.syncPercent(),
      };
    }
    return null;
  },

  bootstrapMb() {
    const p = this.wizard.progress || {};
    const mb = (n) => Math.round((n || 0) / 1048576);
    return mb(p.bytes) + ' of ' + mb(p.size) + ' MB';
  },

  /* ------------------------------------------------------- guidance engine */

  /** Exactly one next step, highest priority first. Never returns null. */
  nextStep() {
    const ws = this.walletState();

    /* 1. Fund-loss risk beats everything. */
    if (this.setup.data_persistent === false) {
      return {
        level: 'danger', icon: 'alert', title: 'Your wallet could be lost',
        text: 'This container’s data directory is not mapped to persistent storage. '
            + 'If the container is removed, any wallet and its coins go with it. '
            + 'Map a host directory to /data before you store real funds.',
        actions: [{ label: 'How to fix this', icon: 'book', go: 'node' }],
      };
    }

    /* 2. Node not answering — nothing else can work. */
    if (this.nodeDown() && !this.bootstrapActive()) {
      return {
        level: 'warning', icon: 'server', title: 'Your node isn’t running',
        text: 'B3 Hive needs the node to read balances and send coins. '
            + 'Starting it takes a few minutes while it scans the block index.',
        actions: [
          { label: 'Start node', icon: 'play', act: 'startNode', primary: true },
          { label: 'Open node view', icon: 'server', go: 'node' },
        ],
      };
    }

    /* 3. No wallet — the single biggest dead end in the old UI. */
    if (ws === 'no-wallet') {
      return {
        level: 'accent', icon: 'wallet', title: 'Create your wallet',
        text: 'A wallet holds your keys and your balance. Create a new one, or load '
            + 'a wallet file you already have. Nothing leaves this machine.',
        actions: [
          { label: 'Create a wallet', icon: 'plus', act: 'openCreateWallet', primary: true },
          { label: 'Load an existing one', icon: 'folder', act: 'openLoadWallet' },
        ],
      };
    }

    /* 4. Queued wizard wallets still waiting on the node. */
    if (this.pendingQueueCount() > 0) {
      const n = this.pendingQueueCount();
      return {
        level: 'info', icon: 'clock',
        title: n === 1 ? 'One wallet is waiting to be set up' : n + ' wallets are waiting to be set up',
        text: 'They are created automatically as soon as your node finishes starting.',
        actions: [{ label: 'View wallets', icon: 'wallet', go: 'wallet' }],
      };
    }

    /* 5. Things the node has flagged. */
    if (this.alertCount > 0) {
      return {
        level: 'warning', icon: 'bell',
        title: this.alertCount === 1 ? 'One thing needs your attention'
                                     : this.alertCount + ' things need your attention',
        text: this.alerts[0]?.message || 'Your node reported something worth a look.',
        actions: [
          { label: 'Review', icon: 'bell', act: 'openAlerts', primary: true },
          { label: 'Dismiss all', icon: 'check', act: 'ackAllAlerts' },
        ],
      };
    }

    /* 6. Idle coins. "Earn rewards" is the verb; "Staking" is the noun. */
    if (ws !== 'no-wallet' && !this.staking.active && isPositiveAmount(this.wallet.balance)) {
      return {
        level: 'accent', icon: 'spark', title: 'Earn rewards on your B3',
        text: 'Staking puts your balance to work producing blocks. Your coins stay in '
            + 'your wallet and you can stop any time.',
        actions: [{ label: 'Start earning', icon: 'spark', go: 'staking', primary: true }],
      };
    }

    /* 7. Remote access without a second factor. */
    if (this.isRemote() && this.session.totp_configured === false) {
      return {
        level: 'info', icon: 'shield', title: 'Protect remote access',
        text: 'You are reaching this wallet over the network. Add an authenticator app '
            + 'so a stolen password alone cannot spend your coins.',
        actions: [{ label: 'Set up 2FA', icon: 'shield', go: 'settings', primary: true }],
      };
    }

    /* 8. Backup nag — lowest priority, and only with real funds at stake. */
    if (ws !== 'no-wallet' && isPositiveAmount(this.wallet.balance) && this.backupIsStale()) {
      return {
        level: 'info', icon: 'download', title: 'Back up your wallet',
        text: 'A backup is the only way to recover your coins if this machine fails. '
            + 'It takes one click and saves next to your chain data.',
        actions: [
          { label: 'Back up now', icon: 'download', act: 'backupWallet', primary: true },
          { label: 'Not now', icon: 'close', act: 'snoozeBackup' },
        ],
      };
    }

    /* 9. Calm. Renders as a one-line strip, not an empty card. */
    return {
      level: 'calm', icon: 'check',
      title: 'Everything’s healthy',
      text: [
        this.synced() ? 'Synced' : null,
        this.chain.connections != null ? this.chain.connections + ' peers' : null,
        this.staking.active ? 'earning rewards' : null,
      ].filter(Boolean).join(' · '),
      actions: [],
    };
  },

  /** Run a nextStep() action. An explicit allowlist rather than `this[name]()`
   *  in the template: inside an Alpine expression `this` is the reactive proxy,
   *  not the raw component, so dynamic dispatch there is fragile. */
  runNextStepAction(action) {
    if (!action) return;
    if (action.go) { this.go(action.go); return; }
    const handlers = {
      startNode: () => this.startNode(),
      openCreateWallet: () => this.openCreateWallet(),
      openLoadWallet: () => this.openLoadWallet(),
      openAlerts: () => this.openAlerts(),
      ackAllAlerts: () => this.ackAllAlerts(),
      backupWallet: () => this.backupWallet(),
      snoozeBackup: () => this.snoozeBackup(),
    };
    handlers[action.act]?.();
  },

  isRemote() { return !LOCALHOSTS.has(location.hostname); },

  backupIsStale() {
    if (this.backupSnoozed) return false;
    const last = Number(localStorage.getItem(BACKUP_KEY) || 0);
    if (!last) return true;
    return (Date.now() - last) > BACKUP_NAG_DAYS * 86400_000;
  },

  markBackedUp() { localStorage.setItem(BACKUP_KEY, String(Date.now())); },
  snoozeBackup() { this.backupSnoozed = true; },

  pendingQueueCount() {
    return (this.walletQueue || []).filter((q) => q.status === 'pending').length;
  },

  /* -------------------------------------------------------------- setup -- */

  /** True while the first-run wizard owns the whole screen.
   *  `wizard.reopened` covers re-running setup from Settings: the backend
   *  marker still says done, and the 15s poll would otherwise kick the user
   *  straight back out of the wizard. */
  setupActive() {
    if (this.wizard.reopened) return true;
    if (!this.setup.checked) return false;
    return this.setup.setup_required === true || this.setup.wizard_done === false;
  },

  /** True while either the wizard or the post-Finish "Setting up" screen owns
   *  the screen. Drives .app[data-setup="active"], which hides all chrome. */
  chromeHidden() { return this.setupActive() || this.applying.active; },

  /* ------------------------------------------------- <html> state attrs -- */

  syncStateAttr() {
    if (this.nodeDown() || this.bootstrapActive()) return 'stopped';
    return this.synced() ? 'synced' : 'ibd';
  },

  /** Mirror derived state onto <html> so CSS can gate without JS in markup. */
  applyStateAttrs() {
    const el = document.documentElement;
    el.setAttribute('data-wallet', this.walletState());
    el.setAttribute('data-sync', this.syncStateAttr());
  },

  /* ------------------------------------------------ staking presentation -- */

  /** Sum of stakes with a given status, as an exact string total. */
  stakeTotals() {
    const out = { ACTIVE: 0n, PENDING: 0n, UNCONFIRMED: 0n, other: 0n };
    for (const s of this.staking.stakes || []) {
      const key = out[s.status] !== undefined ? s.status : 'other';
      const m = String(s.amount ?? '0').match(/^(\d+)(?:\.(\d*))?$/);
      if (!m) continue;
      out[key] += BigInt(m[1] + (m[2] || '').padEnd(9, '0').slice(0, 9));
    }
    const toStr = (u) => {
      const s = u.toString().padStart(10, '0');
      return s.slice(0, -9) + '.' + s.slice(-9);
    };
    return {
      active: toStr(out.ACTIVE),
      pending: toStr(out.PENDING + out.UNCONFIRMED),
      total: toStr(out.ACTIVE + out.PENDING + out.UNCONFIRMED + out.other),
      count: (this.staking.stakes || []).length,
    };
  },

  /** Why can't I stake? Answered explicitly — never a disabled button with
   *  no explanation (persona: Staker Sam). */
  stakingBlocker() {
    if (this.nodeDown()) return 'Your node is still starting.';
    if (this.walletState() === 'no-wallet') return 'You need a wallet loaded first.';
    if (!this.synced()) return 'Your node is still catching up — staking works best once synced.';
    if (!isPositiveAmount(this.wallet.balance) && !isPositiveAmount(this.stakeTotals().total)) {
      return 'You need some confirmed B3 before you can stake.';
    }
    return null;
  },

  stakeStatusClass(status) {
    if (status === 'ACTIVE') return 'badge-success';
    if (status === 'PENDING') return 'badge-warning';
    return 'badge-info';
  },

  stakeStatusText(status) {
    const map = {
      ACTIVE: 'Earning',
      PENDING: 'Activating',
      UNCONFIRMED: 'Confirming',
    };
    return map[status] || (status || 'Unknown');
  },

  /* -------------------------------------------------- activity labelling -- */

  txLabel(category) {
    const map = {
      send: 'Sent', receive: 'Received', generate: 'Block reward',
      immature: 'Block reward', stake: 'Staking reward', orphan: 'Orphaned',
    };
    return map[category] || (category
      ? category.charAt(0).toUpperCase() + category.slice(1) : 'Transaction');
  },

  txIsIncoming(category) {
    return ['receive', 'generate', 'immature', 'stake'].includes(category);
  },

  txIcon(category) {
    if (category === 'send') return 'arrow-up';
    if (category === 'stake' || category === 'generate' || category === 'immature') return 'spark';
    if (category === 'orphan') return 'alert';
    return 'arrow-down';
  },

  txLeadClass(category) {
    if (category === 'send') return 'out';
    if (category === 'stake' || category === 'generate' || category === 'immature') return 'stake';
    return 'in';
  },

  /** Pending/immature coins need explaining, not just a number. */
  balanceChips() {
    const chips = [];
    if (isPositiveAmount(this.wallet.pending)) {
      chips.push({
        cls: 'badge-warning', icon: 'clock',
        label: fmtAmount(this.wallet.pending, { unit: true, maxDecimals: 4 }) + ' pending',
        hint: 'Received but not yet confirmed by enough blocks to spend.',
      });
    }
    if (isPositiveAmount(this.wallet.immature)) {
      chips.push({
        cls: 'badge-info', icon: 'spark',
        label: fmtAmount(this.wallet.immature, { unit: true, maxDecimals: 4 }) + ' maturing',
        hint: 'Block rewards become spendable after they mature.',
      });
    }
    if (isPositiveAmount(this.stakeTotals().total)) {
      chips.push({
        cls: 'badge-accent', icon: 'spark',
        label: fmtAmount(this.stakeTotals().total, { unit: true, maxDecimals: 2 }) + ' staked',
        hint: 'Locked in stakes and earning rewards. Unstake to make it spendable.',
      });
    }
    return chips;
  },
};
