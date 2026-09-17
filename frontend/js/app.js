/* B3 Hive - Alpine.js app store. No build step, no framework, just Alpine. */

function b3app() {
  return {
    // -- State ---------------------------------------------------------------
    session: { authenticated: false, wallet_unlocked: false, totp_configured: false },
    walletMgmt: { loaded: [], on_disk: [], persistent: true,
                  show: false, busy: false,
                  createName: '', createPw: '', createPw2: '',
                  loadName: '', backupBusy: false, backupPath: '',
                  unloadTarget: '', unloadArm: false, error: '' },
    view: 'dashboard',
    theme: localStorage.getItem('b3-theme') || 'dark',
		mode: localStorage.getItem('b3-mode') || 'simple',

    // Login form
    loginPw: '', loginStep2: false, loginBusy: false, loginErr: '', totpCode: '',
    // Wallet
    unlockPw: '', unlockBusy: false,
    wallet: { balance: null, pending: null, txs: [], loaded: false },
    // Chain
    chain: { blocks: null, connections: null, progress: null, mempool: null, finality: null, sync: null, syncErr: false },
    // Staking
    staking: { loading: false, busy: false, active: false, weight: null, info: null, stakes: [],
        settings: null, settingsBusy: false, settingsSaved: false, ack: false, passphrase: '',
        unstakeBusy: false, unstakePreview: null, unstakeTarget: null, revokeArmed: false,
        cons: null, consBusy: false, consPreview: null, consBusyExec: false },
 // Assets (FN Coin / FlowMesh)
 assets: { loaded: false, list: [], fn: null, markets: [], validatorBusy: false },
    // System (versions + daemon log)
    system: { info: null, logs: [], logBusy: false, logCount: 200, logFilter: 'all', logNote: '' },
    // Send
    sendAddr: '', sendAmt: '', sendBusy: false, sendErr: '',
    sendPreview: null, sendResult: null,
    // Settings
    totpBusy: false, totpSetup: null,
 // Receive + address book
 receive: { label: '', busy: false, generated: null },
 book: { addresses: [], loading: false },
 utxos: { list: [], open: false },
 // Sign/verify + passphrase change + peers
 signv: { address: '', message: '', sig: '', vaddress: '', vsig: '', vmessage: '', busy: false, verified: null },
 pwchange: { old: '', new: '', repeat: '', busy: false },
 peers: { list: [], open: false },
 txDetail: null,
 // Batch actions (user-definable recipes)
 batch: {
  recipes: [], loading: false, busy: false, err: '',
  editing: false, editId: null,
  form: { name: '', sources: '', destination: '', min_utxo_value: '', sort: 'smallest', inputs_per_tx: '', max_batches: '', min_output: '', fee_mode: 'estimate', fee_rate: '' },
  preview: null, results: null,
 },
    // Toast
    toast: null, toastType: 'success',
 alerts: [], showAlerts: false, alertCount: 0,
 // Setup wizard (first-run bootstrap + node config)
 wizard: { step: 1, status: null, bootstraps: [], manifestBusy: false, starting: false,
 syncChoice: null, // 'bootstrap' | 'scratch' | 'keep' | null
 freshChain: null, bootTarget: null, wipeChain: false,
 progress: { phase: 'idle' }, progressTimer: null,
 conf: null, confForm: {}, 
 finishing: false,
 secPw: '', secPw2: '', secErr: '', walletQueue: [],
 wallet: { loaded: [], reachable: false, createName: '', createPw: '', createPw2: '', loadName: '', busy: false } },

    // -- Init ----------------------------------------------------------------
    init() {
      this.setTheme(this.theme);
		this.setMode(this.mode);
      this.checkSession();
      this._poll = setInterval(() => this.poll(), 15000);
    this._alertPoll = setInterval(() => { if (this.session.authenticated) this.loadAlerts(); }, 30000);
    },

    // -- API helper ----------------------------------------------------------
    async api(path, opts) {
      opts = opts || {};
      const headers = Object.assign({}, opts.headers || {});
      // CSRF double-submit: read the cookie and mirror it into a header.
      const m = document.cookie.match(/(?:^|; )b3_csrf=([^;]+)/);
      if (m) headers['x-csrf-token'] = m[1];
      if (opts.body) headers['Content-Type'] = 'application/json';
      const res = await fetch(path, { method: opts.method || 'GET', headers: headers, body: opts.body });
      if (res.status === 401) { this.session.authenticated = false; throw new Error('unauthenticated'); }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || res.statusText);
      return data;
    },

    // -- Session -------------------------------------------------------------
    async checkSession() {
      try {
        const s = await this.api('/api/auth/status');
        this.session = s;
        if (s.authenticated) { await this.checkWizard(); this.poll(); }
      } catch (e) { this.session.authenticated = false; }
    },

    // -- Login / 2FA ---------------------------------------------------------
    async doLogin() {
      this.loginErr = ''; this.loginBusy = true;
      try {
        const r = await this.api('/api/auth/login', {
          method: 'POST', body: JSON.stringify({ password: this.loginPw })
        });
        this.loginStep2 = r.totp_required;
        if (!r.totp_required) { this.session.authenticated = true; await this.checkWizard(); this.poll(); }
      } catch (e) { this.loginErr = e.message; }
      this.loginBusy = false;
    },

    async do2fa() {
      this.loginErr = ''; this.loginBusy = true;
      try {
        await this.api('/api/auth/2fa', {
          method: 'POST', body: JSON.stringify({ code: this.totpCode })
        });
        this.session.authenticated = true; this.loginStep2 = false; await this.checkWizard(); this.poll();
      } catch (e) { this.loginErr = e.message; }
      this.loginBusy = false;
    },

    async doLogout() {
      try { await this.api('/api/auth/logout', { method: 'POST' }); } catch (e) {}
      this.session = { authenticated: false, wallet_unlocked: false, totp_configured: false };
      this.loginPw = ''; this.totpCode = ''; this.loginStep2 = false;
    },

    // -- Poll ----------------------------------------------------------------
    async poll() {
      if (!this.session.authenticated) return;
      this.refreshChain();
      this.refreshWallet();
      this.refreshStaking();
      this.refreshSession();
      this.checkWizard();
    },

    // -- System (About / Node log) ------------------------------------------
    async loadSystemInfo() {
      try { this.system.info = await this.api('/api/system/info'); } catch (e) {}
    },

    async loadLogs() {
      this.system.logBusy = true; this.system.logNote = '';
      try {
        const r = await this.api('/api/system/logs?lines=' + this.system.logCount);
        this.system.logs = r.lines || [];
        this.system.logNote = r.note || '';
      } catch (e) { this.system.logs = []; this.system.logNote = ''; }
      this.system.logBusy = false;
    },

    filteredLogs() {
      const f = this.system.logFilter;
      if (f === 'all') return this.system.logs;
      // b3coind lines carry level markers: WARNING, Error, ERROR
      const pat = f === 'error' ? /error|ERROR/ : /WARNING|warn/;
      return this.system.logs.filter(l => pat.test(l));
    },

    openSettings() {
      this.view = 'settings';
      this.loadSystemInfo();
      this.loadLogs();
    },

    async refreshSession() {
      try { this.session = await this.api('/api/auth/status'); } catch (e) {}
    },

    async refreshChain() {
      try {
        const d = await this.api('/api/chain/summary');
        this.chain.blocks = d.blockchain.blocks;
        this.chain.connections = d.network.connections;
        this.chain.progress = d.blockchain.verificationprogress;
 if (d.sync) { this.chain.sync = d.sync; this.chain.syncErr = false; }
 else { this.chain.sync = null; this.chain.syncErr = true; }
        this.chain.mempool = d.mempool.size;
      } catch (e) {}
      try {
        const f = await this.api('/api/chain/finality');
        this.chain.finality = f.finality;
      } catch (e) {}
    },

    async refreshWallet() {
 try {
 const d = await this.api('/api/setup/wallet/status');
 this.wallet.loaded = (d.reachable && d.loaded && d.loaded.length > 0);
 } catch (e) { this.wallet.loaded = false; }
      try {
        const d = await this.api('/api/wallet/info');
        this.wallet.balance = d.balances.mine.trusted;
        this.wallet.pending = d.balances.mine.untrusted_pending;
      } catch (e) {}
      try {
        const d = await this.api('/api/wallet/history?count=25');
        this.wallet.txs = d.transactions || [];
      } catch (e) {}
    },

    async refreshStaking() {
      try {
        const d = await this.api('/api/chain/staking');
        const info = d.staking || {};
        this.staking.info = info;
        const loop = info.staking || info;
        this.staking.active = !!(loop.running !== undefined ? loop.running : loop.staking);
        this.staking.weight = info.weight || loop.weight || null;
        this.staking.stakes = info.stakes || [];
      } catch (e) { this.staking.info = null; }
      try {
        this.staking.settings = await this.api('/api/staking/settings');
      } catch (e) { /* not logged in yet */ }
      try {
        this.staking.cons = await this.api('/api/staking/consolidation/settings');
      } catch (e) { /* not logged in yet */ }
    },

        // -- Assets (FN Coin / FlowMesh) -----------------------------------------
        async refreshAssets() {
            try {
                const d = await this.api('/api/assets');
                this.assets.list = d.assets || [];
                this.assets.fn = d.fn || null;
                this.assets.loaded = true;
            } catch (e) { this.assets.list = []; this.assets.fn = null; this.assets.loaded = true; }
            try {
                const d = await this.api('/api/assets/markets');
                this.assets.markets = d.markets || [];
            } catch (e) { this.assets.markets = []; }
        },

        async validatorAction(action) {
            this.assets.validatorBusy = true;
            try {
                await this.api('/api/assets/validator/' + action, { method: 'POST' });
                this.showToast('FlowMesh validator ' + (action === 'start' ? 'started' : 'stopped'));
            } catch (e) { this.showToast(e.message, 'danger'); }
            this.assets.validatorBusy = false;
        },

        
    // -- Wallet unlock/lock --------------------------------------------------
    async doUnlock() {
      this.unlockBusy = true;
      try {
        await this.api('/api/wallet/unlock', {
          method: 'POST', body: JSON.stringify({ passphrase: this.unlockPw })
        });
        this.unlockPw = '';
        await this.refreshSession();
        this.showToast('Wallet unlocked');
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.unlockBusy = false;
    },

    async doLock() {
      try {
        await this.api('/api/wallet/lock', { method: 'POST' });
        await this.refreshSession();
        this.showToast('Wallet locked');
      } catch (e) { this.showToast(e.message, 'danger'); }
    },

    // -- Wallet management (create / load / migrate / backup) ----------------
    async refreshWalletMgmt() {
      try {
        const r = await this.api('/api/wallet/manage');
        this.walletMgmt.loaded = r.loaded || [];
        this.walletMgmt.on_disk = r.on_disk || [];
        this.walletMgmt.persistent = r.persistent_data;
        this.walletMgmt.error = '';
      } catch (e) { this.walletMgmt.error = e.message; }
    },
    toggleWalletMgmt() {
      this.walletMgmt.show = !this.walletMgmt.show;
      if (this.walletMgmt.show) { this.refreshWalletMgmt(); }
    },
    async createWalletMgmt() {
      if (this.walletMgmt.createPw !== this.walletMgmt.createPw2) {
        this.showToast('Passphrases do not match', 'danger'); return;
      }
      if (this.walletMgmt.createPw.length < 8) {
        this.showToast('Passphrase must be at least 8 characters', 'danger'); return;
      }
      this.walletMgmt.busy = true;
      try {
        await this.api('/api/wallet/manage/create', {
          method: 'POST', body: JSON.stringify({
            wallet_name: this.walletMgmt.createName, passphrase: this.walletMgmt.createPw
          })
        });
        this.showToast('Wallet created: ' + this.walletMgmt.createName);
        this.walletMgmt.createName = ''; this.walletMgmt.createPw = ''; this.walletMgmt.createPw2 = '';
        await this.refreshWalletMgmt();
        await this.refreshWallet();
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.walletMgmt.busy = false;
    },
    async loadWalletMgmt(name) {
      this.walletMgmt.busy = true;
      try {
        await this.api('/api/wallet/manage/load', {
          method: 'POST', body: JSON.stringify({ filename: name || this.walletMgmt.loadName })
        });
        this.showToast('Wallet loaded: ' + (name || this.walletMgmt.loadName));
        this.walletMgmt.loadName = '';
        await this.refreshWalletMgmt();
        await this.refreshWallet();
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.walletMgmt.busy = false;
    },
    async unloadWalletMgmt(name) {
      this.walletMgmt.busy = true;
      try {
        await this.api('/api/wallet/manage/unload', {
          method: 'POST', body: JSON.stringify({ filename: name })
        });
        this.showToast('Wallet unloaded: ' + name);
        this.walletMgmt.unloadTarget = ''; this.walletMgmt.unloadArm = false;
        await this.refreshWalletMgmt();
        await this.refreshWallet();
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.walletMgmt.busy = false;
    },
    async backupWalletMgmt() {
      this.walletMgmt.backupBusy = true;
      try {
        const r = await this.api('/api/wallet/manage/backup', { method: 'POST' });
        this.walletMgmt.backupPath = r.path || '';
        this.showToast('Backup saved');
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.walletMgmt.backupBusy = false;
    },

    // -- Send flow -----------------------------------------------------------
    async doSendPreview() {
      this.sendErr = ''; this.sendBusy = true; this.sendPreview = null; this.sendResult = null;
      try {
        this.sendPreview = await this.api('/api/wallet/send', {
          method: 'POST',
          body: JSON.stringify({
            recipients: [{ address: this.sendAddr, amount: this.sendAmt }],
            confirm: false
          })
        });
      } catch (e) { this.sendErr = e.message; }
      this.sendBusy = false;
    },

    async doSendConfirm() {
      this.sendBusy = true;
      try {
        this.sendResult = await this.api('/api/wallet/send', {
          method: 'POST',
          body: JSON.stringify({
            recipients: [{ address: this.sendAddr, amount: this.sendAmt }],
            confirm: true
          })
        });
        this.showToast('Transaction broadcast: ' + this.sendResult.txid);
      } catch (e) { this.sendErr = e.message; this.showToast(e.message, 'danger'); }
      this.sendBusy = false;
    },

    // -- Staking -------------------------------------------------------------
    async doStartStaking() {
      this.staking.busy = true;
      try {
        await this.api('/api/wallet/staking/start', { method: 'POST' });
        await this.refreshStaking();
        this.showToast('Staking started');
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.busy = false;
    },

    async doStopStaking() {
      this.staking.busy = true;
      try {
        await this.api('/api/wallet/staking/stop', { method: 'POST' });
        await this.refreshStaking();
        this.showToast('Staking stopped');
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.busy = false;
    },

    // -- One-click unstake (two-phase, mirrors the Send flow) -----------------
    async doUnstakePreview(stake) {
      this.staking.unstakeTarget = stake;
      this.staking.unstakePreview = null;
      this.staking.unstakeBusy = true;
      try {
        this.staking.unstakePreview = await this.api('/api/staking/unstake', {
          method: 'POST',
          body: JSON.stringify({ txid: stake.txid, vout: stake.vout, confirm: false })
        });
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.unstakeBusy = false;
    },

    async doUnstakeConfirm() {
      if (!this.staking.unstakePreview || !this.staking.unstakeTarget) return;
      const target = this.staking.unstakeTarget;
      const preview = this.staking.unstakePreview;
      this.staking.unstakeBusy = true;
      try {
        const result = await this.api('/api/staking/unstake', {
          method: 'POST',
          body: JSON.stringify({ txid: target.txid, vout: target.vout,
                                  confirm: true, confirm_token: preview.confirm_token })
        });
        this.showToast('Unstaked: transaction broadcast (' + result.txid + ')');
        this.staking.unstakePreview = null;
        this.staking.unstakeTarget = null;
        await this.refreshStaking();
        await this.refreshWallet();
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.unstakeBusy = false;
    },

    doUnstakeCancel() {
      this.staking.unstakePreview = null;
      this.staking.unstakeTarget = null;
    },

    // -- Autostake settings (Advanced) ---------------------------------------
    async saveStakingSettings() {
      this.staking.settingsBusy = true;
      this.staking.settingsSaved = false;
      const s = this.staking.settings || {};
      try {
        this.staking.settings = await this.api('/api/staking/settings', {
          method: 'POST',
          body: JSON.stringify({
            autostake_enabled: !!s.autostake_enabled,
            autostake_target: s.autostake_target || '0',
            autostake_reserve: s.autostake_reserve || '0',
            passphrase: this.staking.passphrase || '',
            acknowledge_risk: !!this.staking.ack
          })
        });
        this.staking.passphrase = '';
        this.staking.settingsSaved = true;
        this.showToast('Staking settings saved');
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.settingsBusy = false;
    },

    async revokeVault() {
      // Two-step inline confirmation (native confirm() banned by design review).
      if (!this.staking.revokeArmed) { this.staking.revokeArmed = true; return; }
      this.staking.revokeArmed = false;
      try {
        await this.api('/api/staking/vault/revoke', { method: 'POST' });
        this.staking.ack = false;
        await this.refreshStaking();
        this.showToast('Passphrase removed; unattended staking disabled');
      } catch (e) { this.showToast(e.message, 'danger'); }
    },

    async runReconcile() {
      this.staking.settingsBusy = true;
      try {
        const r = await this.api('/api/staking/reconcile', { method: 'POST' });
        if (r.ran) {
          this.showToast('Reconcile ran' + (r.topped_up ? ' - topped up ' + r.topped_up + ' B3' : ''));
        } else {
          this.showToast('Reconcile skipped: ' + (r.reason || 'nothing to do'), 'warning');
        }
        await this.refreshStaking();
        await this.refreshWallet();
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.settingsBusy = false;
    },

    // -- Consolidation sweep (Advanced) ---------------------------------------
    async saveConsolidation() {
      this.staking.consBusy = true;
      try {
        this.staking.cons = await this.api('/api/staking/consolidation/settings', {
          method: 'POST', body: JSON.stringify(this.staking.cons) });
        this.showToast('Consolidation settings saved');
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.consBusy = false;
    },

    async previewConsolidation() {
      this.staking.consBusy = true;
      this.staking.consPreview = null;
      try {
        this.staking.consPreview = await this.api('/api/staking/consolidation/preview',
          { method: 'POST' });
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.consBusy = false;
    },

    async executeConsolidation() {
      this.staking.consBusyExec = true;
      try {
        const r = await this.api('/api/staking/consolidation/execute', {
          method: 'POST', body: JSON.stringify({
            confirm_token: this.staking.consPreview.confirm_token }) });
        this.showToast('Consolidation done: ' + r.results.length + ' batch(es) broadcast');
        this.staking.consPreview = null;
        await this.refreshStaking();
        await this.refreshWallet();
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.staking.consBusyExec = false;
    },

    cancelConsolidation() { this.staking.consPreview = null; },

    // -- Settings / TOTP -----------------------------------------------------
    async doTotpSetup() {
      this.totpBusy = true;
      try {
        const data = await this.api('/api/auth/totp/setup', { method: 'POST' });
        this.totpSetup = data;
        // Render QR code on the canvas after Alpine updates the DOM.
        this.$nextTick(() => {
          const canvas = document.getElementById('totp-qr');
          if (canvas && typeof QRCode !== 'undefined') {
            canvas.innerHTML = '';
            new QRCode(canvas, { text: data.provisioning_uri, width: 200, height: 200 });
          }
        });
      } catch (e) { this.showToast(e.message, 'danger'); }
      this.totpBusy = false;
    },

    // -- Batch actions --------------------------------------------------------
 async refreshBatch() {
  this.batch.loading = true;
  try {
   const d = await this.api('/api/batch/recipes');
   this.batch.recipes = d.recipes || [];
  } catch (e) { this.batch.err = e.message; }
  this.batch.loading = false;
 },

 batchFormToRecipe() {
  const sources = this.batch.form.sources.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
  const recipe = {
   name: this.batch.form.name,
   filters: { sources: sources, sort: this.batch.form.sort || 'smallest' },
   action: { type: 'consolidate', destination: this.batch.form.destination },
  };
  const opt = (v, key, inner) => {
   if (v !== '' && v !== null) {
    const parts = key.split('.');
    let o = recipe;
    for (const p of parts.slice(0, -1)) o = o[p];
    o[parts[parts.length - 1]] = inner ? inner(v) : v;
   }
  };
  opt(this.batch.form.min_utxo_value, 'filters.min_utxo_value');
  opt(this.batch.form.inputs_per_tx, 'limits.inputs_per_tx', v => parseInt(v, 10));
  opt(this.batch.form.max_batches, 'limits.max_batches', v => parseInt(v, 10));
  opt(this.batch.form.min_output, 'limits.min_output');
  const fees = { mode: this.batch.form.fee_mode || 'estimate' };
  if (this.batch.form.fee_rate !== '') fees.fee_rate = this.batch.form.fee_rate;
  if (fees.mode === 'estimate') fees.fee_target = 6;
  recipe.fees = fees;
  return recipe;
 },

 async doBatchSave() {
  this.batch.err = ''; this.batch.busy = true;
  try {
   const recipe = this.batchFormToRecipe();
   if (this.batch.editing) {
    await this.api('/api/batch/recipes/' + this.batch.editId, {
     method: 'PUT', body: JSON.stringify(recipe)
    });
    this.showToast('Recipe updated');
   } else {
    await this.api('/api/batch/recipes', {
     method: 'POST', body: JSON.stringify(recipe)
    });
    this.showToast('Recipe saved');
   }
   this.batch.editing = false; this.batch.editId = null;
   this.batch.preview = null; this.batch.results = null;
   this.batch.form = { name: '', sources: '', destination: '', min_utxo_value: '', sort: 'smallest', inputs_per_tx: '', max_batches: '', min_output: '', fee_mode: 'estimate', fee_rate: '' };
   await this.refreshBatch();
  } catch (e) { this.batch.err = e.message; this.showToast(e.message, 'danger'); }
  this.batch.busy = false;
 },

 batchEdit(r) {
  const rec = r.recipe;
  this.batch.editing = true; this.batch.editId = r.id;
  this.batch.preview = null; this.batch.results = null; this.batch.err = '';
  this.batch.form = {
   name: r.name,
   sources: (rec.filters.sources || []).join('\n'),
   destination: rec.action.destination,
   min_utxo_value: rec.filters.min_utxo_value || '',
   sort: rec.filters.sort || 'smallest',
   inputs_per_tx: rec.limits && rec.limits.inputs_per_tx !== undefined ? String(rec.limits.inputs_per_tx) : '',
   max_batches: rec.limits && rec.limits.max_batches !== undefined ? String(rec.limits.max_batches) : '',
   min_output: rec.limits && rec.limits.min_output !== undefined ? rec.limits.min_output : '',
   fee_mode: rec.fees && rec.fees.mode ? rec.fees.mode : 'estimate',
   fee_rate: rec.fees && rec.fees.fee_rate ? rec.fees.fee_rate : '',
  };
 },

 async batchDelete(r) {
  if (!confirm('Delete recipe "' + r.name + '"?')) return;
  this.batch.busy = true;
  try {
   await this.api('/api/batch/recipes/' + r.id, { method: 'DELETE' });
   this.showToast('Recipe deleted');
   if (this.batch.editId === r.id) {
    this.batch.editing = false; this.batch.editId = null;
    this.batch.preview = null; this.batch.results = null;
   }
   await this.refreshBatch();
  } catch (e) { this.showToast(e.message, 'danger'); }
  this.batch.busy = false;
 },

 async doBatchPreview(r) {
  this.batch.err = ''; this.batch.busy = true; this.batch.preview = null; this.batch.results = null;
  try {
   this.batch.preview = await this.api('/api/batch/recipes/' + r.id + '/preview', { method: 'POST' });
  } catch (e) { this.batch.err = e.message; }
  this.batch.busy = false;
 },

 async doBatchExecute(r) {
  if (!this.session.wallet_unlocked) {
   this.batch.err = 'Unlock the wallet first (Wallet view).';
   return;
  }
  if (!this.batch.preview || !this.batch.preview.confirm_token) return;
  if (!confirm('Execute "' + r.name + '"? This will broadcast ' + (this.batch.preview.batches || []).length + ' transaction(s).')) return;
  this.batch.busy = true;
  try {
   this.batch.results = await this.api('/api/batch/recipes/' + r.id + '/execute', {
    method: 'POST', body: JSON.stringify({ confirm_token: this.batch.preview.confirm_token })
   });
   this.showToast('Batch executed');
   await this.refreshWallet();
  } catch (e) { this.batch.err = e.message; this.showToast(e.message, 'danger'); }
  this.batch.busy = false;
 },

 async openTx(txid) {
 try {
 const d = await this.api('/api/wallet/tx/' + txid);
 this.txDetail = d.transaction;
 } catch (e) { this.showToast(e.message, 'danger'); }
 },

 // -- Receive / address book ---------------------------------------------
 async doReceive() {
 this.receive.busy = true;
 try {
 this.receive.generated = await this.api('/api/wallet/receive', {
 method: 'POST', body: JSON.stringify({ label: this.receive.label })
 });
 this.$nextTick(() => {
 const canvas = document.getElementById('addr-qr');
 if (canvas && typeof QRCode !== 'undefined') {
 canvas.innerHTML = '';
 new QRCode(canvas, { text: this.receive.generated.address, width: 180, height: 180 });
 }
 });
 await this.refreshBook();
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.receive.busy = false;
 },

 async refreshBook() {
 this.book.loading = true;
 try {
 const d = await this.api('/api/wallet/book');
 this.book.addresses = d.addresses;
 } catch (e) {}
 this.book.loading = false;
 },

 async refreshUtxos() {
 if (!this.utxos.open) return;
 try {
 const d = await this.api('/api/wallet/utxos');
 this.utxos.list = d.utxos;
 } catch (e) {}
 },

 async refreshPeers() {
 if (!this.peers.open) return;
 try {
 const d = await this.api('/api/chain/peers');
 this.peers.list = d.peers;
 } catch (e) {}
 },

 copyText(text) {
 navigator.clipboard.writeText(text).then(
 () => this.showToast('Copied to clipboard'),
 () => this.showToast('Copy failed', 'danger')
 );
 },

 // -- Sign / verify message ------------------------------------------------
 async doSignMessage() {
 this.signv.busy = true; this.signv.sig = '';
 try {
 const d = await this.api('/api/wallet/signmessage', {
 method: 'POST',
 body: JSON.stringify({ address: this.signv.address, message: this.signv.message })
 });
 this.signv.sig = d.signature;
 this.showToast('Message signed');
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.signv.busy = false;
 },

 async doVerifyMessage() {
 this.signv.busy = true; this.signv.verified = null;
 try {
 const d = await this.api('/api/wallet/verifymessage', {
 method: 'POST',
 body: JSON.stringify({ address: this.signv.vaddress, signature: this.signv.vsig, message: this.signv.vmessage })
 });
 this.signv.verified = d.valid;
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.signv.busy = false;
 },

 // -- Passphrase change -----------------------------------------------------
 async doPassphraseChange() {
 if (this.pwchange.new !== this.pwchange.repeat) {
 this.showToast('New passphrases do not match', 'danger');
 return;
 }
 if (!confirm('Change the wallet passphrase?')) return;
 this.pwchange.busy = true;
 try {
 await this.api('/api/wallet/passphrase-change', {
 method: 'POST',
 body: JSON.stringify({ old_passphrase: this.pwchange.old, new_passphrase: this.pwchange.new })
 });
 this.pwchange.old = ''; this.pwchange.new = ''; this.pwchange.repeat = '';
 await this.refreshSession();
 this.showToast('Passphrase changed - re-unlock the wallet');
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.pwchange.busy = false;
 },


 // -- Simple/Advanced mode (single global toggle) --------------------------
 setMode(m) {
  this.mode = m;
  localStorage.setItem('b3-mode', m);
  document.documentElement.setAttribute('data-mode', m);
 },
 toggleMode() { this.setMode(this.mode === 'simple' ? 'advanced' : 'simple'); },
 // -- Theme ---------------------------------------------------------------
    setTheme(t) {
      this.theme = t;
      localStorage.setItem('b3-theme', t);
      document.documentElement.setAttribute('data-theme', t);
    },
    toggleTheme() { this.setTheme(this.theme === 'dark' ? 'light' : 'dark'); },

 // -- Alerts --------------------------------------------------------------
 async loadAlerts() {
 if (!this.session.authenticated) return;
 try {
 const r = await this.api('/api/alerts?unacked=true');
 this.alerts = r.alerts || [];
 this.alertCount = r.count || 0;
 } catch (e) { /* silent */ }
 },
 toggleAlerts() {
 this.showAlerts = !this.showAlerts;
 if (this.showAlerts) this.loadAlerts();
 },
 async ackAlert(id) {
 try {
 await this.api('/api/alerts/' + id + '/ack', { method: 'POST' });
 this.showToast('Alert acknowledged');
 this.loadAlerts();
 } catch (e) { this.showToast(e.message, 'danger'); }
 },
 async ackAllAlerts() {
 try {
 await this.api('/api/alerts/ack-all', { method: 'POST' });
 this.showToast('All alerts acknowledged');
 this.loadAlerts();
 } catch (e) { this.showToast(e.message, 'danger'); }
 },

    // -- Setup wizard --------------------------------------------------------
 // Block navigation away from the wizard until setup is complete.
 setView(v) {
 if (this.wizard.status && !this.wizard.status.wizard_done && v !== 'wizard' && v !== 'settings') {
 this.showToast('Complete the setup wizard first', 'warning');
 return;
 }
 this.view = v;
 if (v === 'receive') this.refreshBook();
 if (v === 'assets') this.refreshAssets();
 if (v === 'batch') this.refreshBatch();
 },

 async checkWizard() {
 if (!this.session.authenticated) return;
 try {
 this.wizard.status = await this.api('/api/setup/status');
 if (this.wizard.status.wallet) this.wizard.wallet.loaded = this.wizard.status.wallet.loaded || [];
 if (!this.wizard.status.wizard_done) {
 this.view = 'wizard';
 this.wizard.conf = this.wizard.status.conf || null;
			 if (this.wizard.status.conf && this.wizard.status.conf.editable
 && Object.keys(this.wizard.confForm).length === 0) {
 this.wizard.confForm = Object.assign({}, this.wizard.status.conf.editable);
 }
 }
 this.refreshWalletQueue();
} catch (e) { /* setup status is best-effort */ }
 },
 async loadBootstraps() {
 this.wizard.manifestBusy = true;
 try {
 const r = await this.api('/api/setup/bootstraps');
 this.wizard.bootstraps = r.bootstraps || [];
 this.wizard.freshChain = r.fresh_chain;
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.manifestBusy = false;
 },
 // --- Stepper helpers (selection deferred to Finish; nothing applied mid-wizard) ---
 selectBootstrap(b) {
 this.wizard.bootTarget = b;
 this.wizard.syncChoice = 'bootstrap';
 },
 selectScratch() {
 this.wizard.syncChoice = 'scratch';
 this.wizard.bootTarget = null;
 },
 selectKeep() {
 this.wizard.syncChoice = 'keep';
 this.wizard.bootTarget = null;
 },
 stepClass(n) {
 if (this.wizard.step > n) return 'step-pill--done';
 if (this.wizard.step === n) return 'step-pill--active';
 return '';
 },
 nextStep() {
if (this.wizard.step === 1) {
// Early error catching (user requirement): validate password NOW.
if (this.wizard.status && this.wizard.status.setup_required) {
if (this.wizard.secPw.length < 8) {
this.showToast('Password must be at least 8 characters', 'danger'); return;
}
if (this.wizard.secPw !== this.wizard.secPw2) {
this.showToast('Passwords do not match', 'danger'); return;
}
}
}
if (this.wizard.step === 3) {
if (!this.wizard.syncChoice) {
this.showToast('Pick a sync method first', 'warning'); return;
}
if (this.wizard.syncChoice === 'bootstrap' && !this.wizard.bootTarget) {
this.showToast('Select a bootstrap snapshot or choose another option', 'warning'); return;
}
if (this.wizard.syncChoice === 'bootstrap' && this.wizard.freshChain === false && !this.wizard.wipeChain) {
this.showToast('Enable "Replace existing chain data" to use a bootstrap', 'warning'); return;
}
}
this.wizard.step = Math.min(this.wizard.step + 1, 4);
},
 async pollProgress() {
 try {
 const p = await this.api('/api/setup/bootstrap/progress');
 this.wizard.progress = p;
 if (p.phase === 'done' || p.phase === 'failed') {
 clearInterval(this.wizard.progressTimer);
 this.wizard.progressTimer = null;
 if (p.phase === 'done') this.showToast('Bootstrap applied — node syncing from height ' + p.height);
 if (p.phase === 'failed') this.showToast('Bootstrap failed: ' + (p.error || 'unknown'), 'danger');
 }
 } catch (e) { /* progress poll is best-effort */ }
 },
     // --- Wizard wallet queue: intents are EXECUTED when the node starts ---
async refreshWalletQueue() {
try {
const r = await this.api('/api/setup/wallet/queue');
this.wizard.walletQueue = r.queue || [];
} catch (e) { /* best-effort */ }
},
async queueWalletCreate() {
if (this.wizard.wallet.createPw !== this.wizard.wallet.createPw2) {
this.showToast('Passphrases do not match', 'danger'); return;
}
if (this.wizard.wallet.createPw.length < 8) {
this.showToast('Passphrase must be at least 8 characters', 'danger'); return;
}
if (!this.wizard.wallet.createName) {
this.showToast('Enter a wallet name', 'warning'); return;
}
this.wizard.wallet.busy = true;
try {
await this.api('/api/setup/wallet/queue/create', {
method: 'POST', body: JSON.stringify({
wallet_name: this.wizard.wallet.createName, passphrase: this.wizard.wallet.createPw
})
});
this.showToast('Wallet queued: ' + this.wizard.wallet.createName + ' — created when the node starts');
this.wizard.wallet.createName = ''; this.wizard.wallet.createPw = ''; this.wizard.wallet.createPw2 = '';
await this.refreshWalletQueue();
} catch (e) { this.showToast(e.message, 'danger'); }
this.wizard.wallet.busy = false;
},
async queueWalletLoad() {
if (!this.wizard.wallet.loadName) {
this.showToast('Enter a wallet filename', 'warning'); return;
}
this.wizard.wallet.busy = true;
try {
await this.api('/api/setup/wallet/queue/load', {
method: 'POST', body: JSON.stringify({ filename: this.wizard.wallet.loadName })
});
this.showToast('Wallet load queued: ' + this.wizard.wallet.loadName);
this.wizard.wallet.loadName = '';
await this.refreshWalletQueue();
} catch (e) { this.showToast(e.message, 'danger'); }
this.wizard.wallet.busy = false;
},
async removeWalletIntent(id) {
try {
await this.api('/api/setup/wallet/queue/' + id + '/remove', { method: 'POST' });
await this.refreshWalletQueue();
} catch (e) { this.showToast(e.message, 'danger'); }
},

 // Finish: apply ALL wizard choices in order, then mark the wizard done.
 // Sequence: 1) node config -> 2) sync method (bootstrap download OR node start)
 // -> 3) wizard complete. The daemon never starts before this point, so
 // config changes never need a restart (user requirement).
 finishSummary() {
const parts = [];
if (this.wizard.status && this.wizard.status.setup_required) parts.push('set your login password');
if (this.wizard.conf && this.wizard.conf.editable) parts.push('apply your node configuration');
if (this.wizard.syncChoice === 'bootstrap') parts.push('download the chain snapshot at height ' + (this.wizard.bootTarget ? this.wizard.bootTarget.height : '?') + ' and verify it');
if (this.wizard.syncChoice === 'scratch') parts.push('start the node syncing the full chain from the beginning');
if (this.wizard.syncChoice === 'keep') parts.push('start the node with the chain data already on disk');
const n = (this.wizard.walletQueue || []).filter(q => q.status === 'pending').length;
if (n > 0) parts.push('create/load ' + n + ' queued wallet' + (n > 1 ? 's' : ''));
parts.push('open the dashboard');
return parts.join(', ');
},
 async finishWizard() {
if (this.wizard.status && this.wizard.status.setup_required) {
if (this.wizard.secPw.length < 8) {
this.showToast('Set your login password (step 1) first', 'danger'); return;
}
if (this.wizard.secPw !== this.wizard.secPw2) {
this.showToast('Passwords do not match', 'danger'); return;
}
}
if (!this.wizard.syncChoice) {
this.showToast('Pick a sync method in the Sync step first', 'warning'); return;
}
this.wizard.finishing = true;
try {
// 0) Password (first run): setting it exits setup mode and closes the
// passwordless local window — log straight in with a real session.
if (this.wizard.status && this.wizard.status.setup_required) {
await this.api('/api/auth/setup-password', {
method: 'POST', body: JSON.stringify({ password: this.wizard.secPw })
});
await this.api('/api/auth/login', {
method: 'POST', body: JSON.stringify({ password: this.wizard.secPw })
});
this.session.authenticated = true;
this.wizard.secPw = ''; this.wizard.secPw2 = '';
}
// 1) Node configuration (safe subset) — applied BEFORE first daemon start.
if (this.wizard.conf && this.wizard.conf.editable) {
await this.api('/api/setup/conf/apply', {
method: 'POST', body: JSON.stringify({ conf: this.wizard.confForm })
});
}
// 2a) Bootstrap: the entrypoint worker downloads + verifies, starts the
// daemon, and reports progress. Queued wallet intents run once it is up.
if (this.wizard.syncChoice === 'bootstrap' && this.wizard.bootTarget) {
const b = this.wizard.bootTarget;
const wipe = (this.wizard.freshChain === false) ? this.wizard.wipeChain : false;
await this.api('/api/setup/bootstrap/start', {
method: 'POST', body: JSON.stringify({ height: b.height, sha256: b.sha256, url: b.url, size: b.size, wipe_chain: wipe })
});
this.pollProgress();
this.wizard.progressTimer = setInterval(() => this.pollProgress(), 2000);
}
// 2b) Sync from scratch / keep existing chain: start the (deferred) daemon.
if (this.wizard.syncChoice === 'scratch' || this.wizard.syncChoice === 'keep') {
await this.api('/api/setup/start-node', { method: 'POST' });
}
// 3) Mark the wizard complete (header/nav reappear).
await this.api('/api/setup/complete', { method: 'POST' });
this.wizard.status.wizard_done = true;
this.view = 'dashboard';
this.showToast('Setup complete — welcome to B3 Hive! The node is starting; queued wallets load automatically.');
this.poll();
} catch (e) { this.showToast(e.message, 'danger'); }
this.wizard.finishing = false;
},

 // -- Dashboard helpers: amounts, node status, sync banner, queue --------
fmtAmount(v) {
if (v === null || v === undefined) return '—';
const n = Number(v);
if (Number.isNaN(n)) return String(v);
return n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 9 });
},
txLabel(cat) {
const map = { send: 'Sent', receive: 'Received', generate: 'Mined', immature: 'Mined', stake: 'Staked', orphan: 'Orphaned' };
return map[cat] || (cat ? cat.charAt(0).toUpperCase() + cat.slice(1) : '—');
},
nodeDown() { return this.chain.blocks === null; },
bootstrapPhase() {
if (this.wizard.progress && this.wizard.progress.phase) return this.wizard.progress.phase;
if (this.wizard.status && this.wizard.status.bootstrap && this.wizard.status.bootstrap.phase) {
return this.wizard.status.bootstrap.phase;
}
return 'idle';
},
bootstrapPct() {
const p = this.wizard.progress || {};
if (p.phase !== 'downloading' || !p.size) return 0;
return Math.min(100, Math.round(((p.bytes || 0) / p.size) * 100));
},
bootstrapMb() {
const p = this.wizard.progress || {};
return Math.round((p.bytes || 0) / 1048576) + ' / ' + Math.round((p.size || 0) / 1048576) + ' MB';
},
nodeStatusHeadline() {
const p = this.bootstrapPhase();
if (p === 'downloading' || p === 'verifying' || p === 'extracting' || p === 'wiping') return 'Preparing your node';
if (this.nodeDown()) return 'Node starting up';
if (this.chain.sync && this.chain.sync.behind > 0) return 'Syncing the B3 chain';
return 'Node status';
},
nodeStatusText() {
const peers = this.chain.connections != null ? this.chain.connections : 0;
if (this.nodeDown()) return 'Node starting…';
if (this.chain.sync && this.chain.sync.behind > 0) {
return 'Syncing ' + this.chain.sync.percent.toFixed(1) + '% · ' + peers + ' peers';
}
return 'Synced · ' + peers + ' peers';
},
syncBannerVisible() {
if (this.wizard.status && !this.wizard.status.wizard_done) return false;
const p = this.bootstrapPhase();
if (p !== 'idle' && p !== 'failed' && p !== 'done') return true;
if (this.nodeDown()) return true;
if (this.chain.sync && this.chain.sync.behind > 0) return true;
return false;
},
pendingQueueCount() {
return (this.wizard.walletQueue || []).filter(q => q.status === 'pending').length;
},

// -- Toast ---------------------------------------------------------------
    showToast(msg, type) {
      this.toast = msg; this.toastType = type || 'success';
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast = null; }, 4000);
    },
  };
}
