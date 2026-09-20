/* B3 Hive — view registry and hash routing.

   One registry drives four surfaces: the desktop rail, the mobile tab bar,
   the "More" sheet, and the ⌘K palette. Adding a view in one place makes it
   reachable everywhere, which is how the app avoids the old problem of
   capabilities existing but being unfindable.

   Views are FLAT — no nested routes. Grouping is presentational only. */

export const VIEWS = {
  home: {
    label: 'Home', icon: 'home', width: 'app',
    title: 'Home', sub: 'Your balance and what needs doing',
  },
  send: {
    label: 'Send', icon: 'arrow-up', width: 'flow',
    title: 'Send B3', sub: 'Pay someone in three steps',
    keywords: 'pay transfer spend',
  },
  receive: {
    label: 'Receive', icon: 'arrow-down', width: 'flow',
    title: 'Receive B3', sub: 'Create an address to be paid at',
    keywords: 'address qr deposit',
  },
  activity: {
    label: 'Activity', icon: 'list', width: 'data',
    title: 'Activity', sub: 'Everything this wallet has sent and received',
    keywords: 'history transactions',
  },
  addresses: {
    label: 'Addresses', icon: 'book', width: 'data',
    title: 'Addresses', sub: 'Your address book and labels',
    keywords: 'labels book contacts',
  },
  staking: {
    label: 'Staking', icon: 'spark', width: 'app',
    title: 'Staking', sub: 'Earn rewards with the B3 you hold',
    keywords: 'earn rewards stake unstake validator',
  },
  automation: {
    label: 'Automation', icon: 'repeat', width: 'app', advanced: true,
    title: 'Staking automation', sub: 'Unattended top-ups and UTXO consolidation',
    keywords: 'autostake consolidation sweep vault reconcile unattended',
  },
  assets: {
    label: 'Assets', icon: 'layers', width: 'app',
    title: 'Assets', sub: 'FN Coin, colored assets and FlowMesh',
    keywords: 'fn coin flowmesh markets colored token',
  },
  wallet: {
    label: 'Wallet', icon: 'wallet', width: 'app',
    title: 'Wallet', sub: 'Wallet files, lock state and backups',
    keywords: 'create load unload backup passphrase lock unlock',
  },
  node: {
    label: 'Node', icon: 'server', width: 'app',
    title: 'Node', sub: 'Health, peers, configuration and logs',
    keywords: 'chain peers logs config finality bridge supply restart sync',
  },
  tools: {
    label: 'Tools', icon: 'tool', width: 'data', advanced: true,
    title: 'Tools', sub: 'Batch actions, message signing and coin control',
    keywords: 'batch recipe sign verify message utxo coin control',
  },
  console: {
    label: 'Console', icon: 'terminal', width: 'data', advanced: true,
    title: 'Expert console', sub: 'Run node RPC commands (trusted networks only)',
    keywords: 'rpc terminal command expert debug console',
  },
  settings: {
    label: 'Settings', icon: 'settings', width: 'flow',
    title: 'Settings', sub: 'Security, appearance and setup',
    keywords: '2fa totp theme dark light mode password about version',
  },
};

/** Desktop rail grouping. Group labels are presentational. */
export const NAV_GROUPS = [
  { id: 'top',    label: null,      views: ['home'] },
  { id: 'money',  label: 'Money',   views: ['send', 'receive', 'activity', 'addresses'] },
  { id: 'earn',   label: 'Earn',    views: ['staking', 'automation'] },
  { id: 'assets', label: 'Assets',  views: ['assets'] },
  { id: 'system', label: 'System',  views: ['wallet', 'node', 'tools', 'console', 'settings'] },
];

/** Mobile thumb-zone bar. Send and Receive are the two money verbs, so they
 *  live here permanently: one tap from anywhere. The bar never changes shape
 *  with wallet state — locked/no-wallet render guided states instead. */
export const TAB_VIEWS = ['home', 'send', 'receive', 'staking'];

/** Everything not in the tab bar, grouped for the "More" sheet. */
export const MORE_GROUPS = [
  { label: 'Money',  views: ['activity', 'addresses'] },
  { label: 'Earn',   views: ['automation'] },
  { label: 'Assets', views: ['assets'] },
  { label: 'System', views: ['wallet', 'node', 'tools', 'console', 'settings'] },
];

const DEFAULT_VIEW = 'home';

export const routerMixin = {

  /** Navigate. Blocked while first-run setup owns the screen. */
  go(view, opts = {}) {
    if (this.setupActive()) return;
    if (!VIEWS[view]) view = DEFAULT_VIEW;

    // Reaching an Advanced-only view by palette or deep link turns Advanced
    // on rather than silently refusing — no dead ends.
    if (VIEWS[view].advanced && this.mode !== 'advanced') this.setMode('advanced');

    this.showMore = false;
    this.showPalette = false;
    this.showAlerts = false;

    const changed = this.view !== view;
    this.view = view;
    if (location.hash.slice(1) !== view) {
      history.pushState(null, '', '#' + view);
    }
    document.title = view === DEFAULT_VIEW
      ? 'B3 Hive' : VIEWS[view].title + ' · B3 Hive';

    if (changed) {
      window.scrollTo({ top: 0, behavior: 'instant' });
      this.onEnterView(view, opts);
    }
  },

  /** Per-view lazy loads. Keeps first paint cheap and avoids polling data
   *  the user is not looking at. */
  onEnterView(view, opts = {}) {
    switch (view) {
      case 'receive':    this.loadAddressBook(); break;
      case 'addresses':  this.loadAddressBook(); this.loadLabels(); break;
      case 'activity':   this.loadHistory(); break;
      case 'assets':     this.loadAssets(); break;
      case 'wallet':     this.loadWalletManage(); break;
      case 'staking':    this.loadStaking(); this.loadValidator(); break;
      case 'automation': this.loadStakingSettings(); this.loadConsolidation(); break;
      case 'tools':      this.loadRecipes(); break;
      case 'console': this.consoleLoad(); break;
      case 'node':       this.loadSystemInfo(); this.loadNodeExtras(); break;
      case 'settings': this.loadSystemInfo(); this.loadTailscale(); break;
      case 'send':       this.resetSend(opts.keep); this.loadAddressBook(); break;
    }
  },

  currentView() { return VIEWS[this.view] || VIEWS[DEFAULT_VIEW]; },

  viewWidthClass() {
    const w = this.currentView().width;
    return w === 'flow' ? 'view--flow' : (w === 'data' ? 'view--data' : '');
  },

  /** Hidden only when the feature genuinely has no meaning on this node. */
  navVisible(id) {
    const v = VIEWS[id];
    if (!v) return false;
    if (v.advanced && this.mode !== 'advanced') return false;
    return true;
  },

  navGroups() {
    return NAV_GROUPS
      .map((g) => ({ ...g, items: g.views.filter((v) => this.navVisible(v)) }))
      .filter((g) => g.items.length);
  },

  moreGroups() {
    return MORE_GROUPS
      .map((g) => ({ ...g, items: g.views.filter((v) => this.navVisible(v)) }))
      .filter((g) => g.items.length);
  },

  tabViews() { return TAB_VIEWS; },

  /** Count chip shown on a nav entry. */
  navCount(id) {
    if (id === 'node' && this.alertCount > 0) return this.alertCount;
    return 0;
  },

  /** Collapsible rail groups (OmniRoute-style information scent, but with
   *  ten obvious labels rather than forty obscure ones, so collapsing is a
   *  convenience rather than a necessity). Persisted per browser. */
  groupCollapsed(id) { return !!this.navCollapsed[id]; },

  toggleGroup(id) {
    this.navCollapsed[id] = !this.navCollapsed[id];
    try {
      localStorage.setItem('b3-nav-collapsed', JSON.stringify(this.navCollapsed));
    } catch { /* private mode */ }
  },

  loadNavCollapsed() {
    try {
      this.navCollapsed = JSON.parse(localStorage.getItem('b3-nav-collapsed') || '{}');
    } catch { this.navCollapsed = {}; }
  },

  /* ---------------------------------------------------------- hash sync -- */

  initRouter() {
    const apply = () => {
      const want = location.hash.slice(1);
      if (this.setupActive()) return;
      if (VIEWS[want]) {
        if (want !== this.view) {
          this.view = want;
          this.onEnterView(want);
        }
      } else if (!VIEWS[this.view]) {
        this.view = DEFAULT_VIEW;
      }
    };
    window.addEventListener('popstate', apply);
    window.addEventListener('hashchange', apply);
    this._applyHash = apply;
  },
};
