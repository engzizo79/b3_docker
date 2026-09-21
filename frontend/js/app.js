/* B3 Hive — Alpine component assembly.

   This file is the entry point (kept at its original path). It owns the state
   shape and composes behaviour from focused mixins; each feature module is
   plain methods that run with `this` bound to the Alpine component, so there
   is no build step and no framework beyond Alpine core.

   LOAD ORDER MATTERS. alpine.min.js self-starts and dispatches `alpine:init`
   during startup, so this module must run BEFORE it. index.html loads this as
   a deferred module first and alpine.min.js after; deferred scripts execute in
   document order, so the `alpine:init` listener below is always registered in
   time. (The previous build relied on a global `function b3app()`, which only
   worked because everything was one non-module file.) */

import { apiMixin } from './core/api.js';
import { stateMixin } from './core/state.js';
import { routerMixin, VIEWS } from './core/router.js';
import * as fmt from './core/format.js';

import { modalMixin } from './ui/modal.js';
import { toastMixin } from './ui/toast.js';
import { paletteMixin } from './ui/palette.js';
import { initTooltips } from './ui/tooltip.js';

import { sessionMixin } from './features/session.js';
import { walletMixin } from './features/wallet.js';
import { sendMixin } from './features/send.js';
import { contactsMixin } from './features/contacts.js';
import { stakingMixin } from './features/staking.js';
import { assetsMixin } from './features/assets.js';
import { batchMixin } from './features/batch.js';
import { nodeMixin } from './features/node.js';
import { alertsMixin } from './features/alerts.js';
import { consoleMixin } from './features/console.js';
import { wizardMixin } from './features/wizard.js';
import { installMixin } from './features/wizard.js';
import { nodeConnMixin } from './features/wizard.js';
import { tailscaleMixin } from './features/tailscale.js';

/** Formatting helpers exposed to markup by name. */
const formatMixin = {
  fmtAmount: fmt.fmtAmount,
  amountDisplay: fmt.amountDisplay,
  amountHtml: fmt.amountHtml,
  truncAddr: fmt.truncAddr,
  isPositiveAmount: fmt.isPositiveAmount,
  isZeroAmount: fmt.isZeroAmount,
  compareAmounts: fmt.compareAmounts,
  subtractAmounts: fmt.subtractAmounts,
  fmtInt: fmt.fmtInt,
  fmtPercent: fmt.fmtPercent,
  fmtBytes: fmt.fmtBytes,
  fmtDuration: fmt.fmtDuration,
  fmtDateTime: fmt.fmtDateTime,
  fmtTime: fmt.fmtTime,
  humanKey: fmt.humanKey,
  truncHash: fmt.truncHash,
  VIEWS,
  view_meta(id) { return VIEWS[id] || {}; },
};

/** Modal confirmation for every irreversible action. window.confirm() is
 *  banned for anything wallet-affecting (brief §7), and the old build still
 *  used it in three places. */
const confirmMixin = {
  /** confirm({title, body, detail?, confirmLabel?, danger?, run}) */
  confirm(opts) {
    this.confirmModal = {
      show: true,
      title: opts.title || 'Are you sure?',
      body: opts.body || '',
      // Callers pass {key, value, mono?} rows or plain strings (a note).
      detail: (opts.detail || []).map((d) => (typeof d === 'string' ? { key: '', value: d } : d)),
      confirmLabel: opts.confirmLabel || 'Confirm',
      danger: !!opts.danger,
      run: opts.run || (() => {}),
    };
  },

  async runConfirm() {
    const run = this.confirmModal.run;
    // Close FIRST: the action may itself raise the 423 passphrase modal, and
    // two stacked dialogs would fight over the focus trap.
    this.confirmModal.show = false;
    try { await run(); } catch { /* actions report their own errors */ }
  },

  cancelConfirm() { this.confirmModal.show = false; },
};

function initialState() {
  return {
    /* -------------------------------------------------------------- shell */
    booted: false,
    view: 'home',
    theme: localStorage.getItem('b3-theme') || 'dark',
    mode: localStorage.getItem('b3-mode') || 'simple',
    showMore: false,
    navCollapsed: {},
    showPalette: false,
    paletteQuery: '',
    paletteIndex: 0,
    toasts: [],
    confirmModal: { show: false, title: '', body: '', detail: [], confirmLabel: '', danger: false, run: null },
    backupSnoozed: false,
    lastTxid: null,

    /* ------------------------------------------------------------ session */
    session: { authenticated: false, wallet_unlocked: false, totp_configured: false },
 sysMode: { checked: false, mode: 'managed', managed: true, node_restart: true, bootstrap: true, daemon_logs: true, daemon_upgrade: true, backup_download: true },
  nodeConn: { loaded: false, choice: null, open: false, busy: false, error: '', done: false, restart: false, form: { mode: 'managed', version: '', extHost: '', extPort: 38647, extUser: '', extPassword: '' } },
    loginPw: '', loginStep2: false, loginBusy: false, loginErr: '', totpCode: '',
    unlockPrompt: { show: false, pw: '', busy: false, err: '', reveal: false, retry: null },
    unlockLeft: 0,
    totpBusy: false, totpSetup: null,
    pwchange: { current: '', next: '', repeat: '', busy: false },

    /* ------------------------------------------------------------- wallet */
    // `checked:false` means "we do not know yet" — distinct from "no wallet",
    // so the UI never flashes an incorrect empty state on first paint.
    walletStatus: { checked: false, reachable: false, loaded: [] },
    wallet: { name: null, balance: null, pending: null, immature: null, info: null },
    history: { txs: [], count: 100, filter: 'all', busy: false, loaded: false },
    txDetail: null,
    receive: { label: '', busy: false, generated: null },
    book: { addresses: [], labels: [], loaded: false, busy: false, error: false, query: '', editing: null, draftLabel: '', hideEmpty: false, sort: { key: null, dir: 'asc' } },
    contacts: { list: [], loaded: false, error: false, busy: false, form: { label: '', address: '' }, saveName: '', editing: null, draft: '' },
    manage: {
      loaded: [], onDisk: [], persistent: true, busy: false, working: false, err: '',
      pane: null, loadName: '', backupBusy: false, backupPath: '', backupFile: '',
      create: { name: '', pw: '', pw2: '', reveal: false },
    },
    utxos: { list: [], busy: false, loaded: false, minconf: 1 },
    signv: {
      address: '', message: '', sig: '', busy: false,
      vaddress: '', vsig: '', vmessage: '', vbusy: false, verified: null,
    },

    /* -------------------------------------------------------------- money */
    send: {
      step: 1, to: '', amount: '', label: '',
      busy: false, err: '', preview: null, result: null,
      pickerOpen: false, pickerQuery: '',
    },

    /* --------------------------------------------------------------- node */
    chain: {
      blocks: null, headers: null, connections: null, mempool: null,
      sync: null, summary: null, finality: null,
      // Why the node is (not) answering, from /api/chain/node-state; null = not asked yet.
      nodeState: null,
    },
    // True while the BACKEND itself cannot be reached (server stopped, proxy up
    // without it, network gone). Distinct from the node not answering.
    backend: { down: false },
    node: {
      info: null, peers: [], bridge: null, supply: null,
      conf: null, confForm: {}, confEditing: false,
      busy: false, working: false, starting: false,
      logs: [], logBusy: false, logNote: '', logCount: 200, logFilter: 'all',
    },
 // Targeted daemon-install flow (upgrade path: wizard done, binary missing)
 install: {
 open: false, busy: false, error: '', done: false,
 version: '', releases: [], releasesBusy: false, releasesErr: '',
 started: false,
 },
  console: {
    input: '', lines: [], history: [], histPos: -1,
    catalog: null, loaded: false, gate: null, gateMsg: '', busy: false,
    mode: null, settings: null, sform: { networks: '', remote_full_access: false }, sbusy: false, max: false,
  },

    /* ---------------------------------------------------------- tailscale */
  ts: { loaded: false, available: true, joined: false, needs_login: false, rekey: false, serve_enabled: false,
    https_url: null, tailnet: null, detail: '', authkey: '', joinOpen: false,
    busy: false, error: '' },

/* ------------------------------------------------------------ staking */
    staking: {
      active: false, available: true, state: null, info: null, stakes: [],
      weight: null, netWeight: null, busy: false, working: false, validator: null,
    },
 startFlow: { show: false, amount: "", err: "", busy: false, confirming: false },
 addStake: { show: false, amount: "", err: "", busy: false },
    unstake: { target: null, preview: null, busy: false },
    autostake: {
      settings: null, form: { enabled: false, target: '', reserve: '' },
      passphrase: '', ack: false, busy: false,
    },
    autoLog: { entries: [], status: null, group: 'automation', state: 'all', q: '',
               hours: 0, busy: false, loaded: false, run: null, runBusy: false },
    cons: { settings: null, form: {}, preview: null, results: null, busy: false, working: false },

    /* ------------------------------------------------------------- assets */
    assets: {
      list: [], fn: null, markets: [], loaded: false, busy: false,
      validatorBusy: false, createSupported: true,
    },
    fnCreate: { open: false, ack: false, address: '', busy: false, result: null },

    /* -------------------------------------------------------------- tools */
    batch: {
      recipes: [], loaded: false, busy: false, working: false, err: '',
      editing: false, editId: null,
      form: {
        name: '', sources: '', destination: '', sort: 'smallest',
        min_utxo_value: '', inputs_per_tx: '', max_batches: '', min_output: '',
        fee_mode: 'estimate', fee_rate: '',
      },
      preview: null, previewFor: null, results: null,
    },

    /* ------------------------------------------------------------- alerts */
    alerts: [], alertCount: 0, showAlerts: false,

    /* -------------------------------------------------------------- setup */
    setup: {
      checked: false, wizard_done: true, setup_required: false,
      fresh_chain: null, data_persistent: true, daemon_deferred: null,
      daemon_binary_missing: false, daemon_mode: 'managed',
      wallet: { loaded: [], reachable: false },
      bootstrap: { phase: 'idle' },
      conf: null,
    },
    walletQueue: [],
    wizard: {
      step: 1, seeded: false, reopened: false,
      pw: '', pw2: '',
      // Sign-in mode: the wizard can reopen when an operator account already
      // exists but the setup marker is missing (restart before Finish,
      // restored data dir). Security then collects the EXISTING login
      // password instead of saying 'nothing to do' while every Finish call
      // would 401.
      signinPw: '', signinTotp: '', signinErr: '',
      signinBusy: false, signinTotpNeeded: false,
      confForm: {}, confExpanded: false,
      bootstraps: [], bootstrapsBusy: false, bootstrapsErr: '',
      // v0.6.0 daemon mode + version picker (Node step)
      daemonMode: 'managed', daemonVersion: '', daemonReleases: [],
      daemonReleasesBusy: false, daemonReleasesErr: '',
      daemonExtHost: '', daemonExtPort: 38647, daemonExtUser: '', daemonExtPassword: '',
      syncChoice: null, bootTarget: null, wipeChain: false, freshChain: null,
      wallet: { mode: null, name: '', pw: '', pw2: '', loadName: '', busy: false },
      progress: { phase: 'idle' }, progressTimer: null,
    },
    applying: { active: false, failed: false, finished: false, allDone: false, tasks: [] },
  };
}

function b3app() {
  return Object.assign(
    initialState(),
    formatMixin,
    apiMixin,
    stateMixin,
    routerMixin,
    modalMixin,
    toastMixin,
    paletteMixin,
    confirmMixin,
    sessionMixin,
    walletMixin,
    sendMixin,
    contactsMixin,
    stakingMixin,
    assetsMixin,
    batchMixin,
    nodeMixin,
    alertsMixin,
    consoleMixin,
    wizardMixin,
    installMixin,
    nodeConnMixin,
 tailscaleMixin,
    {
       async fetchSysMode() {
 try {
 const r = await this.api('/api/system/mode');
 this.sysMode = Object.assign({ checked: true }, r);
 } catch (e) { this.sysMode.checked = true; }
 },

 init() {
        this.setTheme(this.theme);
        this.setMode(this.mode);
        this.loadNavCollapsed();
        this.applyStateAttrs();
        initTooltips();
        this.initShortcuts();
        // Deep link before auth is known, so the view is right the moment
        // the session resolves.
        const want = location.hash.slice(1);
        if (VIEWS[want]) this.view = want;
        this.checkSession();
 this.fetchSysMode();
      },
    },
  );
}

document.addEventListener('alpine:init', () => {
  window.Alpine.data('b3app', b3app);
});
