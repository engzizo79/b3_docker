/* B3 Hive - Alpine.js app store. No build step, no framework, just Alpine. */

function b3app() {
  return {
    // -- State ---------------------------------------------------------------
    session: { authenticated: false, wallet_unlocked: false, totp_configured: false },
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
        unstakeBusy: false, unstakePreview: null, unstakeTarget: null, revokeArmed: false },
 // Assets (FN Coin / FlowMesh)
 assets: { loaded: false, list: [], fn: null, markets: [], validatorBusy: false },
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
 wizard: { status: null, bootstraps: [], manifestBusy: false, starting: false,
 progress: { phase: 'idle' }, progressTimer: null,
 conf: null, confForm: {}, confBusy: false, restartBusy: false,
 secPw: '', secPw2: '', secBusy: false, secSet: false, secErr: '',
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
        if (s.authenticated) this.poll();
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
        if (!r.totp_required) { this.session.authenticated = true; this.poll(); }
      } catch (e) { this.loginErr = e.message; }
      this.loginBusy = false;
    },

    async do2fa() {
      this.loginErr = ''; this.loginBusy = true;
      try {
        await this.api('/api/auth/2fa', {
          method: 'POST', body: JSON.stringify({ code: this.totpCode })
        });
        this.session.authenticated = true; this.loginStep2 = false; this.poll();
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
 } catch (e) { /* setup status is best-effort */ }
 },
 async loadBootstraps() {
 this.wizard.manifestBusy = true;
 try {
 const r = await this.api('/api/setup/bootstraps');
 this.wizard.bootstraps = r.bootstraps || [];
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.manifestBusy = false;
 },
 async startBootstrap(b) {
 if (!confirm('Download bootstrap at height ' + b.height + '? ~' +
 Math.round(b.size / 1048576) + ' MB. The daemon will be stopped during download.')) return;
 this.wizard.starting = true;
 try {
 await this.api('/api/setup/bootstrap/start', {
 method: 'POST', body: JSON.stringify({ height: b.height, sha256: b.sha256, url: b.url, size: b.size })
 });
 this.showToast('Bootstrap started');
 this.pollProgress();
 this.wizard.progressTimer = setInterval(() => this.pollProgress(), 2000);
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.starting = false;
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
 async applyConf() {
 this.wizard.confBusy = true;
 try {
 await this.api('/api/setup/conf/apply', {
 method: 'POST', body: JSON.stringify({ conf: this.wizard.confForm })
 });
 this.showToast('Configuration applied — restart the node to take effect');
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.confBusy = false;
 },
 async restartNode() {
 if (!confirm('Restart b3coind to apply configuration changes?')) return;
 this.wizard.restartBusy = true;
 try {
 await this.api('/api/setup/restart-node', { method: 'POST' });
 this.showToast('Node restart queued');
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.restartBusy = false;
 },
 async createWallet() {
 if (this.wizard.wallet.createPw !== this.wizard.wallet.createPw2) {
 this.showToast('Passphrases do not match', 'danger'); return;
 }
 if (this.wizard.wallet.createPw.length < 8) {
 this.showToast('Passphrase must be at least 8 characters', 'danger'); return;
 }
 this.wizard.wallet.busy = true;
 try {
 await this.api('/api/setup/wallet/create', {
 method: 'POST', body: JSON.stringify({
 wallet_name: this.wizard.wallet.createName, passphrase: this.wizard.wallet.createPw
 })
 });
 this.showToast('Wallet created: ' + this.wizard.wallet.createName);
 this.wizard.wallet.loaded.push(this.wizard.wallet.createName);
 this.wizard.wallet.createName = ''; this.wizard.wallet.createPw = ''; this.wizard.wallet.createPw2 = '';
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.wallet.busy = false;
 },
 async loadWallet() {
 this.wizard.wallet.busy = true;
 try {
 await this.api('/api/setup/wallet/load', {
 method: 'POST', body: JSON.stringify({ filename: this.wizard.wallet.loadName })
 });
 this.showToast('Wallet loaded: ' + this.wizard.wallet.loadName);
 this.wizard.wallet.loaded.push(this.wizard.wallet.loadName);
 this.wizard.wallet.loadName = '';
 } catch (e) { this.showToast(e.message, 'danger'); }
 this.wizard.wallet.busy = false;
 },
 async setSecurityPassword() {
 this.wizard.secErr = "";
 if (this.wizard.secPw.length < 8) { this.wizard.secErr = "Password must be at least 8 characters"; return; }
 if (this.wizard.secPw !== this.wizard.secPw2) { this.wizard.secErr = "Passwords do not match"; return; }
 this.wizard.secBusy = true;
 try {
 await this.api("/api/auth/setup-password", { method: "POST", body: JSON.stringify({ password: this.wizard.secPw }) });
 this.wizard.secSet = true;
 // Auto-login: setting the password exits setup mode, closing the
 // passwordless local window. Establish a REAL session right away so
 // the rest of the wizard (complete) works without a login screen.
 await this.api("/api/auth/login", { method: "POST", body: JSON.stringify({ password: this.wizard.secPw }) });
 this.wizard.secPw = ""; this.wizard.secPw2 = "";
 this.showToast("Password set — remote access enabled");
 this.session.authenticated = true;
 this.poll();
 } catch (e) { this.wizard.secErr = e.message; }
 this.wizard.secBusy = false;
 },

 async completeWizard(startNode) {
 if (this.wizard.status && this.wizard.status.setup_required && !this.wizard.secSet) {
 this.showToast("Set a login password in the Security step first", "danger"); return;
 }
 try {
 if (startNode) await this.api('/api/setup/start-node', { method: 'POST' });
 await this.api('/api/setup/complete', { method: 'POST' });
 this.wizard.status.wizard_done = true;
 this.view = 'dashboard';
 this.showToast('Setup complete — welcome to B3 Hive!');
 } catch (e) { this.showToast(e.message, 'danger'); }
 },

 // -- Toast ---------------------------------------------------------------
    showToast(msg, type) {
      this.toast = msg; this.toastType = type || 'success';
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast = null; }, 4000);
    },
  };
}
