/* B3 Hive — ⌘K command palette.

   A *command* palette, not just a jump list: it navigates AND performs. This
   is the safety net for discoverability — every capability in the app is
   findable by typing a word for it, so nothing is lost to progressive
   disclosure.

   Gated commands stay listed but disabled, with the reason shown inline
   ("needs an unlocked wallet"). Hiding them would recreate the original
   complaint: features that exist but cannot be found. */

import { VIEWS } from '../core/router.js';
import { truncAddr } from '../core/format.js';

/** Fixed group order. Keeps each heading contiguous and unique. */
const GROUP_ORDER = ['Go to', 'Do', 'Send to'];

export const paletteMixin = {

  openPalette() {
    if (this.setupActive()) return;
    this.paletteQuery = '';
    this.paletteIndex = 0;
    this.showPalette = true;
  },

  closePalette() { this.showPalette = false; },

  /** Everything the palette can do. Built fresh so gating reflects live state. */
  paletteCommands() {
    const cmds = [];

    /* --- Go to ----------------------------------------------------------- */
    for (const [id, v] of Object.entries(VIEWS)) {
      cmds.push({
        key: 'go:' + id,
        group: 'Go to',
        label: v.title,
        hint: v.sub,
        icon: v.icon,
        terms: [id, v.label, v.title, v.sub, v.keywords || ''].join(' '),
        run: () => this.go(id),
      });
    }

    /* --- Do -------------------------------------------------------------- */
    const ws = this.walletState();
    const noWallet = ws === 'no-wallet';
    const nodeDown = this.nodeDown();

    const add = (c) => cmds.push({ group: 'Do', ...c, terms: [c.label, c.hint || '', c.terms || ''].join(' ') });

    add({
      key: 'do:send', label: 'Send B3', icon: 'arrow-up',
      hint: 'Pay someone', terms: 'pay transfer spend',
      why: noWallet ? 'Needs a wallet' : (nodeDown ? 'Node is not running' : null),
      run: () => this.go('send'),
    });
    add({
      key: 'do:receive', label: 'New receive address', icon: 'arrow-down',
      hint: 'Create an address to be paid at', terms: 'deposit qr',
      why: noWallet ? 'Needs a wallet' : null,
      run: () => { this.go('receive'); this.$nextTick(() => this.newAddress()); },
    });
    add({
      key: 'do:stake-start', label: 'Start staking', icon: 'spark',
      hint: 'Begin earning rewards', terms: 'earn rewards',
      why: this.staking.active ? 'Already running' : this.stakingBlocker(),
      run: () => this.startStaking(),
    });
    add({
      key: 'do:stake-stop', label: 'Stop staking', icon: 'stop',
      hint: 'Stop producing blocks', terms: 'earn rewards halt',
      why: this.staking.active ? null : 'Not currently running',
      run: () => this.stopStaking(),
    });
    add({
      key: 'do:lock', label: 'Lock wallet', icon: 'lock',
      hint: 'Require the passphrase again', terms: 'secure',
      why: ws === 'unlocked' ? null : 'Wallet is not unlocked',
      run: () => this.lockWallet(),
    });
    add({
      key: 'do:unlock', label: 'Unlock wallet', icon: 'unlock',
      hint: 'Enter your passphrase now', terms: 'passphrase',
      why: ws === 'locked' ? null : (noWallet ? 'Needs a wallet' : 'Already unlocked'),
      run: () => this.askUnlock(),
    });
    add({
      key: 'do:backup', label: 'Back up wallet', icon: 'download',
      hint: 'Save a copy of your keys', terms: 'export safety',
      why: noWallet ? 'Needs a wallet' : null,
      run: () => this.backupWallet(),
    });
    add({
      key: 'do:create-wallet', label: 'Create a wallet', icon: 'plus',
      hint: 'Start a new wallet file', terms: 'new',
      why: nodeDown ? 'Node is not running' : null,
      run: () => this.openCreateWallet(),
    });
    add({
      key: 'do:load-wallet', label: 'Load an existing wallet', icon: 'folder',
      hint: 'Open a wallet file already on disk', terms: 'migrate import restore',
      why: nodeDown ? 'Node is not running' : null,
      run: () => this.openLoadWallet(),
    });
    add({
      key: 'do:sign', label: 'Sign a message', icon: 'pen',
      hint: 'Prove you control an address', terms: 'verify signature',
      why: noWallet ? 'Needs a wallet' : null,
      run: () => this.go('tools', { focus: 'sign' }),
    });
    add({
      key: 'do:consolidate', label: 'Preview UTXO consolidation', icon: 'repeat',
      hint: 'Merge small outputs to cut future fees', terms: 'sweep dust combine',
      why: noWallet ? 'Needs a wallet' : null,
      run: () => { this.go('automation'); this.$nextTick(() => this.previewConsolidation()); },
    });
    add({
      key: 'do:restart-node', label: 'Restart node', icon: 'power',
      hint: 'Apply configuration changes', terms: 'reboot daemon',
      run: () => this.restartNode(),
    });
    add({
      key: 'do:theme', label: this.theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme',
      icon: this.theme === 'dark' ? 'sun' : 'moon',
      hint: 'Change appearance', terms: 'dark light appearance colour color',
      run: () => this.toggleTheme(),
    });
    add({
      key: 'do:mode', label: this.mode === 'simple' ? 'Turn on Advanced mode' : 'Turn off Advanced mode',
      icon: 'sliders',
      hint: this.mode === 'simple' ? 'Show coin control, batch and raw details' : 'Hide expert options',
      terms: 'expert simple advanced',
      run: () => this.toggleMode(),
    });
    add({
      key: 'do:logout', label: 'Sign out', icon: 'logout',
      hint: 'End this session', terms: 'exit quit',
      run: () => this.logout(),
    });

    /* --- Recent addresses ------------------------------------------------ */
    for (const a of (this.book.addresses || []).slice(0, 6)) {
      cmds.push({
        key: 'addr:' + a.address,
        group: 'Send to',
        label: a.label || truncAddr(a.address),
        hint: a.label ? truncAddr(a.address) : 'From your address book',
        icon: 'book',
        terms: [a.label || '', a.address].join(' '),
        why: noWallet ? 'Needs a wallet' : null,
        run: () => { this.sendTo(a.address, a.label); },
      });
    }

    return cmds;
  },

  /** Subsequence match, so "stst" finds "Start staking".
   *
   *  Results are returned GROUPED, in a fixed group order, with score order
   *  preserved inside each group. Sorting purely by score interleaved the
   *  groups, which made the heading for a group appear more than once and
   *  produced duplicate x-for keys — Alpine then failed to reconcile the
   *  list at all. Grouping here keeps one heading per group by construction.
   */
  paletteResults() {
    const q = this.paletteQuery.trim().toLowerCase();
    const all = this.paletteCommands();

    let picked;
    if (!q) {
      picked = all.filter((c) => c.group !== 'Send to').slice(0, 14);
    } else {
      const scored = [];
      for (const c of all) {
        const score = matchScore(c.terms.toLowerCase(), q, (c.label || '').toLowerCase());
        if (score > 0) scored.push({ c, score });
      }
      scored.sort((a, b) => b.score - a.score);
      picked = scored.slice(0, 20).map((s) => s.c);
    }

    const out = [];
    for (const g of GROUP_ORDER) {
      for (const c of picked) if (c.group === g) out.push(c);
    }
    // Any future group not listed in GROUP_ORDER still shows up, contiguously.
    for (const c of picked) if (!GROUP_ORDER.includes(c.group)) out.push(c);
    return out;
  },

  /** Results flattened into rows with group headings, for rendering.
   *  Keys are namespaced so a heading can never collide with a command key. */
  paletteRows() {
    const rows = [];
    let group = null;
    this.paletteResults().forEach((c, i) => {
      if (c.group !== group) {
        group = c.group;
        rows.push({ heading: group, key: 'h:' + group });
      }
      rows.push({ cmd: c, i, key: 'c:' + c.key });
    });
    return rows;
  },

  paletteMove(delta) {
    const n = this.paletteResults().length;
    if (!n) return;
    this.paletteIndex = (this.paletteIndex + delta + n) % n;
    this.$nextTick(() => {
      document.querySelector('.palette-item[aria-selected="true"]')
        ?.scrollIntoView({ block: 'nearest' });
    });
  },

  paletteRun(cmd) {
    const c = cmd || this.paletteResults()[this.paletteIndex];
    if (!c || c.why) return;
    this.showPalette = false;
    c.run();
  },

  /* ------------------------------------------------------ global shortcut */

  initShortcuts() {
    window.addEventListener('keydown', (e) => {
      // ⌘K / Ctrl+K anywhere.
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (this.showPalette) this.closePalette(); else this.openPalette();
        return;
      }
      if (e.key === 'Escape' && this.showPalette) {
        e.preventDefault();
        this.closePalette();
      }
    });
  },
};

function matchScore(hay, q, label) {
  if (label.startsWith(q)) return 1000 - label.length;
  const direct = hay.indexOf(q);
  if (direct !== -1) return 500 - direct;
  // subsequence
  let i = 0, gaps = 0;
  for (const ch of q) {
    const at = hay.indexOf(ch, i);
    if (at === -1) return 0;
    gaps += at - i;
    i = at + 1;
  }
  return Math.max(1, 200 - gaps);
}
