import { seal } from '../core/envelope.js';

/* B3 Hive — first-run setup.

   Hard requirements this implements (brief §5.1 / §5.2):

   1. On first run the wizard is the ONLY thing rendered — no rail, no tab
      bar, no dashboard flash. The shell enforces that via
      .app[data-setup="active"]; this module owns the content.
   2. NOTHING is applied until Finish. Every step only records a choice. The
      daemon is started by Finish and not before, which is why node config
      never needs a restart during setup.
   3. The Finish sequence is ported verbatim in order — it is load-bearing:
        0) if setup_required: POST /api/auth/setup-password, then /api/auth/login
        1) if conf.editable:  POST /api/setup/conf/apply
        2a) bootstrap chosen: POST /api/setup/bootstrap/start, then poll progress
        2b) scratch or keep:  POST /api/setup/start-node
        3) POST /api/setup/complete
   4. After Finish the user sees a live "Setting up" checklist, not silence
      followed by a log dive.

   Step order matches the brief: Security -> Node -> Sync -> Wallet -> Review. */

const STEPS = [
  { id: 1, key: 'security', label: 'Security' },
  { id: 2, key: 'node',     label: 'Node' },
  { id: 3, key: 'sync',     label: 'Sync' },
  { id: 4, key: 'wallet',   label: 'Wallet' },
  { id: 5, key: 'review',   label: 'Review' },
];

/** Sensible values for someone who should not have to care. */
const RECOMMENDED_CONF = {
  txindex: '1',
  listen: '1',
  maxconnections: '125',
  bantime: '86400',
};

export const wizardMixin = {

  wizardSteps() { return STEPS; },

  /** Poll /api/setup/status — the source of truth for first-run detection. */
  async checkSetup() {
    try {
      const s = await this.api('/api/setup/status');
      this.setup = {
        checked: true,
        wizard_done: !!s.wizard_done,
        setup_required: !!s.setup_required,
        fresh_chain: s.fresh_chain,
        data_persistent: s.data_persistent,
        daemon_deferred: (s.daemon_deferred === undefined) ? null : s.daemon_deferred,
        daemon_binary_missing: !!s.daemon_binary_missing,
        daemon_mode: s.daemon_mode || 'managed',
        wallet: s.wallet || { loaded: [], reachable: false },
        bootstrap: s.bootstrap || { phase: 'idle' },
        conf: s.conf || null,
      };
      if (s.wallet) {
        this.walletStatus = {
          checked: true,
          reachable: !!s.wallet.reachable,
          loaded: s.wallet.loaded || [],
        };
      }
      await this.loadWalletQueue();
    } catch {
      this.setup.checked = true;
    }
  },

  /** Seed the wizard's forms from the node's current state. */
  loadWizard() {
    if (this.wizard.seeded) return;
    this.wizard.seeded = true;
    const conf = this.setup.conf;
    if (conf?.editable) {
      this.wizard.confForm = { ...RECOMMENDED_CONF, ...conf.editable };
    } else {
      this.wizard.confForm = { ...RECOMMENDED_CONF };
    }
    // A fresh chain can sync from scratch; an existing chain defaults to
    // keeping what is already downloaded.
    this.wizard.syncChoice = this.setup.fresh_chain === false ? 'keep' : null;
    this.loadBootstraps();
 this.loadDaemonReleases();
  },

  /* ------------------------------------------------------ step 1 security */

  passwordStrength() {
    const p = this.wizard.pw || '';
    let score = 0;
    if (p.length >= 8) score++;
    if (p.length >= 14) score++;
    if (/[^A-Za-z0-9]/.test(p) || (/[A-Z]/.test(p) && /[a-z]/.test(p) && /\d/.test(p))) score++;
    if (p.length >= 20) score++;
    return Math.min(4, score);
  },

  passwordStrengthWord() {
    return ['Too short', 'Weak', 'Fair', 'Good', 'Strong'][this.passwordStrength()];
  },

  /* The wizard reopened but an operator account already exists: the user
     must SIGN IN (existing password) before any protected Finish call. */
  wizardSigninNeeded() {
    return !this.setup.setup_required && !this.session.authenticated;
  },

  async wizardSignin() {
    this.wizard.signinErr = ''; this.wizard.signinBusy = true;
    try {
      const env = await seal(this.wizard.signinPw, 'b3hive-login');
      const r = await this.api('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify(env ? { env } : { password: this.wizard.signinPw }),
      });
      if (r.totp_required) {
        this.wizard.signinTotpNeeded = true;
      } else {
        this.session.authenticated = true;
        this.wizard.signinPw = '';
      }
    } catch (e) {
      this.wizard.signinErr = e.message;
    }
    this.wizard.signinBusy = false;
  },

  async wizardSignin2fa() {
    this.wizard.signinErr = ''; this.wizard.signinBusy = true;
    try {
      await this.api('/api/auth/2fa', {
        method: 'POST', body: JSON.stringify({ code: this.wizard.signinTotp.trim() }),
      });
      this.session.authenticated = true;
      this.wizard.signinTotpNeeded = false;
      this.wizard.signinPw = ''; this.wizard.signinTotp = '';
    } catch (e) {
      this.wizard.signinErr = e.message;
      this.wizard.signinTotp = '';
    }
    this.wizard.signinBusy = false;
  },

  securityOk() {
    if (!this.setup.setup_required) return !this.wizardSigninNeeded();
    return this.wizard.pw.length >= 8 && this.wizard.pw === this.wizard.pw2;
  },

  securityProblem() {
    if (!this.setup.setup_required) return null;
    if (!this.wizard.pw) return null;
    if (this.wizard.pw.length < 8) return 'At least 8 characters.';
    if (this.wizard.pw2 && this.wizard.pw !== this.wizard.pw2) return 'The two passwords do not match.';
    return null;
  },

  /* ---------------------------------------------------------- step 2 node */

  confKeys() { return Object.keys(this.setup.conf?.editable || RECOMMENDED_CONF); },

  /** One tap for the 95% who should not think about txindex. */
  useRecommendedConf() {
    this.wizard.confForm = { ...this.wizard.confForm, ...RECOMMENDED_CONF };
    this.wizard.confExpanded = false;
    this.showToast('Using recommended node settings');
  },

  confHelp(key) {
    return {
      txindex: 'Keep a full index of every transaction so old payments stay searchable. Uses more disk.',
      listen: 'Accept incoming connections from other nodes. Helps the network and speeds up your own sync.',
      port: 'The port other nodes connect to you on. Must match the port published in docker-compose.yml (B3_P2P_PORT) — changing one without the other means no inbound connections.',
      maxconnections: 'How many peers to talk to at once. More peers sync faster but use more bandwidth.',
      proxy: 'Route node traffic through a proxy, for example 127.0.0.1:9050 for Tor. Leave empty to connect directly.',
      bantime: 'How long a misbehaving peer stays blocked, in seconds. 86400 is one day.',
    }[key] || 'A node setting.';
  },

  confLabel(key) {
    return {
      txindex: 'Full transaction index',
      listen: 'Accept incoming connections',
      port: 'P2P port',
      maxconnections: 'Maximum peers',
      proxy: 'Proxy',
      bantime: 'Peer ban time (seconds)',
    }[key] || key;
  },

  confIsToggle(key) { return key === 'txindex' || key === 'listen'; },

  toggleConf(key) {
    this.wizard.confForm[key] = this.wizard.confForm[key] === '1' ? '0' : '1';
  },

   /* ---------------------------------- v0.6.0 daemon mode picker */

 async loadDaemonReleases() {
 if (this.wizard.daemonReleasesBusy) return;
 this.wizard.daemonReleasesBusy = true; this.wizard.daemonReleasesErr = '';
 try {
 const r = await this.api('/api/setup/daemon/releases');
 this.wizard.daemonReleases = (r.releases || []).filter((x) => x.meets_minimum);
 if (!this.wizard.daemonVersion) {
 this.wizard.daemonVersion = (r.installed || '') || ((r.releases || [])[0] || {}).tag || '';
 }
 } catch (e) {
 this.wizard.daemonReleasesErr = 'Could not fetch the release list.';
 }
 this.wizard.daemonReleasesBusy = false;
 },

 daemonChoiceOk() {
 if (this.wizard.daemonMode === 'external') {
 return Boolean(this.wizard.daemonExtHost) && Boolean(this.wizard.daemonExtUser) && Boolean(this.wizard.daemonExtPassword) && (this.wizard.daemonExtPort > 0);
 }
 return true;
 },

 async saveDaemonChoice() {
 await this.api('/api/setup/daemon/choose', {
 method: 'POST',
 body: JSON.stringify({
 mode: this.wizard.daemonMode,
 version: this.wizard.daemonVersion || null,
 ext_host: this.wizard.daemonExtHost || null,
 ext_port: Number(this.wizard.daemonExtPort) || null,
 ext_user: this.wizard.daemonExtUser || null,
 ext_password: this.wizard.daemonExtPassword || null,
 }),
 });
 },

/* ---------------------------------------------------------- step 3 sync */

  async loadBootstraps() {
    this.wizard.bootstrapsBusy = true;
    try {
      const r = await this.api('/api/setup/bootstraps');
      this.wizard.bootstraps = r.bootstraps || [];
      this.wizard.freshChain = r.fresh_chain;
    } catch (e) {
      this.wizard.bootstraps = [];
      this.wizard.bootstrapsErr = 'Could not reach the snapshot list. You can still sync from scratch.';
    }
    this.wizard.bootstrapsBusy = false;
  },

  /** The option we actively recommend, badged in the UI. */
  recommendedSync() {
    if (this.setup.fresh_chain === false) return 'keep';
    return this.wizard.bootstraps.length ? 'bootstrap' : 'scratch';
  },

  chooseSync(choice) {
    this.wizard.syncChoice = choice;
    if (choice !== 'bootstrap') {
      this.wizard.bootTarget = null;
      this.wizard.wipeChain = false;
    } else if (!this.wizard.bootTarget && this.wizard.bootstraps.length) {
      // Default to the newest snapshot so the common case is one tap.
      this.wizard.bootTarget = this.wizard.bootstraps
        .slice().sort((a, b) => (b.height || 0) - (a.height || 0))[0];
    }
  },

  selectBootstrap(b) {
    this.wizard.bootTarget = b;
    this.wizard.syncChoice = 'bootstrap';
  },

  sortedBootstraps() {
    return this.wizard.bootstraps.slice().sort((a, b) => (b.height || 0) - (a.height || 0));
  },

  syncOk() {
    const w = this.wizard;
    if (!w.syncChoice) return false;
    if (w.syncChoice === 'bootstrap') {
      if (!w.bootTarget) return false;
      // Consent is mandatory when it would destroy existing chain data.
      if (this.setup.fresh_chain === false && !w.wipeChain) return false;
    }
    return true;
  },

  syncProblem() {
    const w = this.wizard;
    if (!w.syncChoice) return 'Choose how this node should get the blockchain.';
    if (w.syncChoice === 'bootstrap' && !w.bootTarget) return 'Pick a snapshot.';
    if (w.syncChoice === 'bootstrap' && this.setup.fresh_chain === false && !w.wipeChain) {
      return 'Using a snapshot replaces the chain data already on disk — confirm that below.';
    }
    return null;
  },

  /* -------------------------------------------------------- step 4 wallet */

  async loadWalletQueue() {
    try {
      const r = await this.api('/api/setup/wallet/queue');
      this.walletQueue = r.queue || [];
    } catch { /* best effort */ }
  },

  wizWalletNameValid() {
    return /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(this.wizard.wallet.name);
  },

  wizWalletReady() {
    const w = this.wizard.wallet;
    return this.wizWalletNameValid() && w.pw.length >= 8 && w.pw === w.pw2;
  },

  /** Wizard wallet actions are QUEUED, not executed: the daemon is still
   *  down at this point, so there is nothing to create a wallet on yet. */
  async queueWalletCreate() {
    const w = this.wizard.wallet;
    if (!this.wizWalletNameValid()) {
      this.showToast('Use letters, numbers, dots, dashes and underscores only', 'danger'); return;
    }
    if (w.pw.length < 8) { this.showToast('The passphrase must be at least 8 characters', 'danger'); return; }
    if (w.pw !== w.pw2) { this.showToast('The passphrases do not match', 'danger'); return; }
    this.wizard.wallet.busy = true;
    try {
      await this.api('/api/setup/wallet/queue/create', {
        method: 'POST',
        body: JSON.stringify({ wallet_name: w.name, passphrase: w.pw }),
      });
      this.showToast('“' + w.name + '” will be created when your node starts');
      this.wizard.wallet = { mode: null, name: '', pw: '', pw2: '', loadName: '', busy: false };
      await this.loadWalletQueue();
    } catch (e) { this.reportError(e); }
    this.wizard.wallet.busy = false;
  },

  async queueWalletLoad() {
    const name = this.wizard.wallet.loadName.trim();
    if (!name) { this.showToast('Enter the wallet filename', 'warning'); return; }
    this.wizard.wallet.busy = true;
    try {
      await this.api('/api/setup/wallet/queue/load', {
        method: 'POST',
        body: JSON.stringify({ filename: name }),
      });
      this.showToast('“' + name + '” will be loaded when your node starts');
      this.wizard.wallet = { mode: null, name: '', pw: '', pw2: '', loadName: '', busy: false };
      await this.loadWalletQueue();
    } catch (e) { this.reportError(e); }
    this.wizard.wallet.busy = false;
  },

  async removeWalletIntent(id) {
    try {
      await this.api('/api/setup/wallet/queue/' + id + '/remove', { method: 'POST' });
      await this.loadWalletQueue();
    } catch (e) { this.reportError(e); }
  },

  /* ------------------------------------------------------- step movement -- */

  stepState(id) {
    if (this.wizard.step > id) return 'done';
    if (this.wizard.step === id) return 'now';
    return '';
  },

  canAdvance() {
    switch (this.wizard.step) {
      case 1: return this.securityOk();
      case 3: return this.syncOk();
      default: return true;
    }
  },

  stepProblem() {
    switch (this.wizard.step) {
      case 1:
        if (this.wizardSigninNeeded()) return 'Sign in with your login password to continue.';
        return this.securityOk() ? null
        : (this.wizard.pw.length < 8 ? 'Choose a password of at least 8 characters.'
                                     : 'The two passwords do not match.');
      case 3: return this.syncProblem();
      default: return null;
    }
  },

  wizardNext() {
    const problem = this.stepProblem();
    if (problem) { this.showToast(problem, 'warning'); return; }
    this.wizard.step = Math.min(STEPS.length, this.wizard.step + 1);
    window.scrollTo({ top: 0, behavior: 'instant' });
  },

  wizardBack() {
    this.wizard.step = Math.max(1, this.wizard.step - 1);
    window.scrollTo({ top: 0, behavior: 'instant' });
  },

  /* ------------------------------------------------------- step 5 review -- */

  /** A checklist of exactly what Finish will do, in the order it happens. */
  reviewPlan() {
    const plan = [];
    if (this.setup.setup_required) {
      plan.push({ icon: 'shield', text: 'Set your login password and sign you in' });
    } else if (!this.session.authenticated) {
      plan.push({ icon: 'shield', text: 'Sign in with your existing login password' });
    } else {
      plan.push({ icon: 'shield', text: 'Keep your existing login password (you are signed in)' });
    }
    if (this.setup.conf?.editable) {
      plan.push({ icon: 'sliders', text: 'Write your node settings to b3coin.conf' });
    }
    const w = this.wizard;
    if (w.syncChoice === 'bootstrap' && w.bootTarget) {
      plan.push({
        icon: 'download',
        text: 'Download and verify the snapshot at block '
            + (w.bootTarget.height ?? '?').toLocaleString()
            + ' (' + Math.round((w.bootTarget.size || 0) / 1048576) + ' MB)',
      });
      if (w.wipeChain) {
        plan.push({ icon: 'alert', text: 'Delete the chain data already on disk', danger: true });
      }
    } else if (w.syncChoice === 'scratch') {
      plan.push({ icon: 'server', text: 'Start your node and sync the whole chain from the beginning' });
    } else if (w.syncChoice === 'keep') {
      plan.push({ icon: 'server', text: 'Start your node using the chain data already on disk' });
    }
    const pending = this.pendingQueueCount();
    if (pending) {
      plan.push({
        icon: 'wallet',
        text: pending === 1 ? 'Set up the wallet you queued' : 'Set up the ' + pending + ' wallets you queued',
      });
    }
    plan.push({ icon: 'check', text: 'Open your wallet' });
    return plan;
  },

  /* ---------------------------------------------------------- the Finish -- */

  async finishWizard() {
    if (this.wizardSigninNeeded()) {
      this.wizard.step = 1;
      this.showToast('Sign in again to finish setup', 'warning');
      return;
    }
    if (this.setup.data_persistent === false) {
      this.showToast('Setup is blocked: the data directory is not persistent. Map a Docker volume to /data and restart the container.', 'danger');
      return;
    }
    // Re-validate: the user can reach Review and then go back and break things.
    if (this.setup.setup_required && !this.securityOk()) {
      this.wizard.step = 1;
      this.showToast('Set your login password first', 'danger');
      return;
    }
    if (!this.syncOk()) {
      this.wizard.step = 3;
      this.showToast(this.syncProblem() || 'Choose a sync method', 'warning');
      return;
    }

    // Build the live checklist the user watches while this runs.
    const tasks = [];
    if (this.setup.setup_required) tasks.push({ key: 'password', label: 'Setting your login password' });
    if (this.setup.conf?.editable) tasks.push({ key: 'conf', label: 'Applying node settings' });
    if (this.wizard.syncChoice === 'bootstrap') {
      tasks.push({ key: 'bootstrap', label: 'Downloading the chain snapshot' });
    } else {
      tasks.push({ key: 'node', label: 'Starting your node' });
    }
    if (this.pendingQueueCount()) tasks.push({ key: 'wallets', label: 'Setting up your wallets' });
    tasks.push({ key: 'done', label: 'Finishing up' });

    this.applying = {
      active: true, failed: false, finished: false, allDone: false,
      tasks: tasks.map((t) => ({ ...t, state: 'pending', detail: '' })),
    };
    const mark = (key, state, detail) => {
      const t = this.applying.tasks.find((x) => x.key === key);
      if (t) { t.state = state; if (detail !== undefined) t.detail = detail; }
    };

    try {
      /* 0) Password. Setting it exits setup mode and closes the passwordless
            local window, so log straight in to get a real session. */
      if (this.setup.setup_required) {
        mark('password', 'active');
        await this.api('/api/auth/setup-password', {
          method: 'POST', body: JSON.stringify({ password: this.wizard.pw }),
        });
        await this.api('/api/auth/login', {
          method: 'POST', body: JSON.stringify({ password: this.wizard.pw }),
        });
        this.session.authenticated = true;
        this.setup.setup_required = false;
        this.wizard.pw = ''; this.wizard.pw2 = '';
        mark('password', 'done');
      }

      /* 0.5) v0.6.0 daemon mode + version choice (Node step). */
 if (this.wizard.daemonMode === 'external' || this.wizard.daemonVersion) {
 mark('conf', 'active', this.wizard.daemonMode === 'managed'
 ? 'Downloading the daemon from GitHub (can take a few minutes)…'
 : undefined);
 await this.saveDaemonChoice();
 mark('conf', 'done', this.wizard.daemonMode === 'managed' ? 'Daemon downloaded.' : '');
 }

/* 1) Node configuration, BEFORE the first daemon start. */
      if (this.setup.conf?.editable) {
        mark('conf', 'active');
        // The backend rejects empty values with 422, so only send real ones.
        const conf = {};
        for (const [k, v] of Object.entries(this.wizard.confForm || {})) {
          const val = String(v ?? '').trim();
          if (val !== '') conf[k] = val;
        }
        if (Object.keys(conf).length) {
          await this.api('/api/setup/conf/apply', {
            method: 'POST', body: JSON.stringify({ conf }),
          });
        }
        mark('conf', 'done', '');
      }

      /* 2a) Bootstrap: the entrypoint supervisor downloads, verifies and
             starts the daemon; we poll and report progress. */
      if (this.wizard.syncChoice === 'bootstrap' && this.wizard.bootTarget) {
        mark('bootstrap', 'active');
        const b = this.wizard.bootTarget;
        const wipe = this.setup.fresh_chain === false ? !!this.wizard.wipeChain : false;
        await this.api('/api/setup/bootstrap/start', {
          method: 'POST',
          body: JSON.stringify({
            height: b.height, sha256: b.sha256, url: b.url,
            size: b.size, wipe_chain: wipe,
          }),
        });
        this.startProgressPolling();
      }

      /* 2b) Scratch or keep: start the deferred daemon. */
      if (this.wizard.syncChoice === 'scratch' || this.wizard.syncChoice === 'keep') {
        mark('node', 'active');
        await this.api('/api/setup/start-node', { method: 'POST' });
        this.setup.daemon_deferred = false;
        this.node.starting = true;
        // Stays ACTIVE until the daemon actually answers; the outcome
        // watcher flips it so the checklist never lies.
        mark('node', 'active', 'It takes a few minutes to scan the block index.');
      }

      /* 3) Mark the wizard complete — this is what releases the app shell. */
      mark('done', 'active');
      await this.api('/api/setup/complete', { method: 'POST' });
      this.setup.wizard_done = true;

      if (this.pendingQueueCount()) {
        mark('wallets', 'active', 'They are created automatically once your node is up.');
      }

      this.applying.finished = true;
      this.startPolling();
      this.pollNow();
      // HONEST completion: the 'done' task stays active (spinner) until the
      // real background work finishes — node answering, queued wallets
      // processed, snapshot applied. The user can enter the app anytime;
      // every screen shows live progress in the sync banner.
      this.watchSetupOutcome();
    } catch (e) {
      this.applying.failed = true;
      const active = this.applying.tasks.find((t) => t.state === 'active');
      if (active) { active.state = 'failed'; active.detail = e.message; }
      this.reportError(e);
    }
  },

  /** Poll until the node answers, queued wallets are processed and the
   * bootstrap snapshot (if chosen) is applied — then flip 'Finishing up'
   * to done with a truthful detail line. 30-minute safety stop; the
   * background work continues regardless of the watcher. */
  watchSetupOutcome() {
    clearInterval(this._setupWatch);
    let ticks = 0;
    const queueWalletsLabel = 'Wallets set up';
    const watch = async () => {
      ticks += 1;
      try {
        await this.checkSetup(); // refreshes bootstrap phase + wallet queue
      } catch { /* best-effort */ }
      const phase = this.bootstrapPhase();
      const nodeUp = !this.nodeDown(); // strict: the daemon must answer
      const queueLeft = this.pendingQueueCount();
      const bootActive = this.bootstrapActive();
      // Flip the 'node' task only once the daemon actually responds.
      const tn = this.applying.tasks.find((x) => x.key === 'node');
      if (tn && tn.state === 'active' && nodeUp) {
        tn.state = 'done';
        tn.label = 'Node started';
        tn.detail = 'It answered and is now syncing.';
      }
      // Flip the bootstrap task if its progress polling already stopped.
      const tb = this.applying.tasks.find((x) => x.key === 'bootstrap');
      if (tb && tb.state === 'active' && phase === 'done') {
        tb.state = 'done';
        tb.label = 'Chain snapshot applied';
        tb.pct = null;
      }
      // Queued wallets are created once the node answers; flip their row
      // as soon as the queue drains so it never spins after the fact.
      const tw = this.applying.tasks.find((x) => x.key === 'wallets');
      if (tw && tw.state === 'active') {
        const failedN = (this.walletQueue || []).filter((q) => q.status === 'failed').length;
        if (nodeUp && queueLeft === 0 && failedN) {
          tw.state = 'failed';
          tw.detail = failedN === 1
            ? 'One wallet could not be created. Check Wallet for details.'
            : failedN + ' wallets could not be created. Check Wallet for details.';
        } else if (nodeUp && queueLeft === 0) {
          tw.state = 'done';
          tw.label = queueWalletsLabel;
          tw.detail = 'Your wallets are created and ready.';
        } else if (nodeUp) {
          tw.detail = 'Creating them now…';
        }
      }
      const t = this.applying.tasks.find((x) => x.key === 'done');
      if (t && t.state === 'active') {
        if (!nodeUp) {
          t.detail = 'Waiting for your node to answer - this can take a few minutes.';
        } else if (bootActive) {
          t.detail = 'Still applying the chain snapshot in the background.';
        } else if (queueLeft > 0) {
          t.detail = queueLeft === 1
            ? 'Setting up 1 wallet that is still waiting for the node.'
            : 'Setting up ' + queueLeft + ' wallets that are still waiting for the node.';
        } else {
          t.state = 'done';
          t.detail = 'Everything is set up and running.';
          this.applying.allDone = true;
          clearInterval(this._setupWatch);
          this.showToast('Setup finished - your node is up to speed.');
          return;
        }
      }
      if (ticks > 900) { // 30 min at 2s; work continues regardless
        clearInterval(this._setupWatch);
      }
    };
    watch();
    this._setupWatch = setInterval(watch, 2000);
  },

  /** Progress polling for the bootstrap download. */
  startProgressPolling() {
    clearInterval(this.wizard.progressTimer);
    const tick = async () => {
      try {
        const p = await this.api('/api/setup/bootstrap/progress');
        this.wizard.progress = p;
        const t = this.applying.tasks?.find((x) => x.key === 'bootstrap');
        if (t) {
          if (p.phase === 'downloading') {
            t.state = 'active';
            t.label = 'Downloading the chain snapshot';
            t.detail = this.bootstrapMb() + ' · ' + this.bootstrapPct() + '%';
            t.pct = this.bootstrapPct();
          } else if (p.phase === 'verifying') {
            t.state = 'active'; t.label = 'Checking the snapshot is genuine'; t.detail = '';
          } else if (p.phase === 'extracting') {
            t.state = 'active'; t.label = 'Unpacking the snapshot'; t.detail = '';
          } else if (p.phase === 'wiping') {
            t.state = 'active'; t.label = 'Clearing the old chain data'; t.detail = '';
          } else if (p.phase === 'done') {
            t.state = 'done';
            t.label = 'Chain snapshot applied';
            t.detail = 'Starting from block ' + (p.height ?? '?');
            t.pct = null;
          } else if (p.phase === 'failed') {
            t.state = 'failed';
            t.detail = p.error || 'The download did not complete.';
          }
        }
        if (p.phase === 'done' || p.phase === 'failed') {
          clearInterval(this.wizard.progressTimer);
          this.wizard.progressTimer = null;
        }
      } catch { /* progress is best-effort */ }
    };
    tick();
    this.wizard.progressTimer = setInterval(tick, 2000);
  },

  /** Leave the setting-up screen for the real app. */
  enterApp() {
    this.applying.active = false;
    this.wizard.reopened = false;
    this.initRouter();
    this.go('home');
    this.startPolling();
    this.pollNow();
  },

  /** Re-run setup later, from Settings. The backend marker stays "done", so a
   *  local flag holds the wizard open against the 15s status poll. */
  reopenWizard() {
    this.wizard.step = 1;
    this.wizard.seeded = false;
    this.wizard.reopened = true;
    this.loadWizard();
    window.scrollTo({ top: 0, behavior: 'instant' });
  },

  /** Abandon a re-opened wizard without changing anything. */
  cancelReopenedWizard() {
    this.wizard.reopened = false;
    this.go('settings');
  },
};

/* -------- Targeted daemon install (upgrade path: wizard done, binary missing) -------- */

export const installMixin = {

  async loadInstallReleases() {
    this.install.releasesBusy = true;
    this.install.releasesErr = '';
    try {
      const r = await this.api('/api/setup/daemon/releases');
      this.install.releases = (r.releases || []).filter(x => x.meets_minimum);
      if (!this.install.version && this.install.releases.length) {
        this.install.version = this.install.releases[0].tag || '';
      }
    } catch (e) {
      this.install.releasesErr = 'Could not load releases. Check your internet connection.';
    } finally {
      this.install.releasesBusy = false;
    }
  },

  openInstallModal() {
    this.install.open = true;
    this.install.busy = false;
    this.install.error = '';
    this.install.done = false;
    this.install.started = false;
    this.loadInstallReleases();
  },

  closeInstallModal() {
    this.install.open = false;
  },

  async doInstall() {
    this.install.busy = true;
    this.install.error = '';
    try {
      await this.api('/api/setup/daemon/choose', {
        method: 'POST',
        body: JSON.stringify({
          mode: 'managed',
          version: this.install.version || null,
        }),
      });
      this.install.done = true;
    } catch (e) {
      this.install.error = e.message || 'Download failed.';
    } finally {
      this.install.busy = false;
    }
  },

  async installAndStart() {
    if (!this.install.done) {
      await this.doInstall();
      if (this.install.error) return;
    }
    this.install.started = true;
    try {
      await this.api('/api/setup/start-node', { method: 'POST' });
      this.setup.daemon_deferred = false;
      this.node.starting = true;
      this.setup.daemon_binary_missing = false;
      this.install.open = false;
      this.showToast('Node software installed - starting up');
    } catch (e) {
      this.install.error = e.message || 'Could not start the node.';
      this.install.started = false;
    }
  }
};

/* -------- Node connection card (Settings): mode indicator + switch -------- */

export const nodeConnMixin = {

  async loadNodeConn() {
    try {
      const c = await this.api('/api/setup/daemon/choice');
      this.nodeConn.choice = c.chosen ? c.choice : null;
    } catch { this.nodeConn.choice = null; }
    this.nodeConn.loaded = true;
  },

  nodeConnMode() {
    if (!this.nodeConn.loaded) return null;
    if (this.nodeConn.choice) return this.nodeConn.choice.mode;
    return this.sysMode.mode || 'managed';
  },

  openNodeConnSwitch() {
    this.nodeConn.open = true;
    this.nodeConn.error = '';
    this.nodeConn.busy = false;
    this.nodeConn.restart = false;
    const m = this.nodeConnMode();
    this.nodeConn.form.mode = (m === 'external') ? 'managed' : 'external';
    this.nodeConn.form.version = (this.nodeConn.choice && this.nodeConn.choice.version) || '';
    this.nodeConn.form.extHost = (this.nodeConn.choice && this.nodeConn.choice.ext_host) || '';
    this.nodeConn.form.extPort = (this.nodeConn.choice && this.nodeConn.choice.ext_port) || 38647;
    this.nodeConn.form.extUser = '';
    this.nodeConn.form.extPassword = '';
    this.loadInstallReleases();
  },

  closeNodeConnSwitch() {
    this.nodeConn.open = false;
  },

  nodeConnFormOk() {
    if (this.nodeConn.form.mode === 'external') {
      return Boolean(this.nodeConn.form.extHost)
        && Boolean(this.nodeConn.form.extUser)
        && Boolean(this.nodeConn.form.extPassword)
        && (Number(this.nodeConn.form.extPort) > 0);
    }
    return Boolean(this.nodeConn.form.version);
  },

  async applyNodeConnSwitch() {
    this.nodeConn.busy = true;
    this.nodeConn.error = '';
    try {
      const body = { mode: this.nodeConn.form.mode };
      if (this.nodeConn.form.mode === 'managed') {
        body.version = this.nodeConn.form.version || null;
      } else {
        body.ext_host = this.nodeConn.form.extHost;
        body.ext_port = Number(this.nodeConn.form.extPort);
        body.ext_user = this.nodeConn.form.extUser;
        body.ext_password = this.nodeConn.form.extPassword;
      }
      const r = await this.api('/api/setup/daemon/choose', {
        method: 'POST', body: JSON.stringify(body),
      });
      this.nodeConn.restart = !!r.backend_restart;
      this.nodeConn.done = true;
    } catch (e) {
      this.nodeConn.error = e.message || 'Switch failed.';
    } finally {
      this.nodeConn.busy = false;
    }
  },
};
