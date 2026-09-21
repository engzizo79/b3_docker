/* B3 Hive — wallet: balances, activity, addresses, wallet files, coin control.

   Wallet management (create / load / unload / backup) is reachable here at
   ANY time, not only inside the first-run wizard — that was one of the
   explicit failures of the previous UI (brief §5.3). */

import { dayBucket, truncAddr, isValidAddress } from '../core/format.js';
import { renderQR } from '../ui/qr.js';

export const walletMixin = {

  renderQR,
  truncAddr,

  /* ------------------------------------------------------------ read state */

  /** Loaded-wallet detection. Separate from lock state, so the UI can tell
   *  "no wallet" from "locked" (brief §5.5). */
  async loadWalletStatus() {
    try {
      const d = await this.api('/api/setup/wallet/status');
      this.walletStatus = {
        checked: true,
        reachable: !!d.reachable,
        loaded: d.loaded || [],
      };
    } catch {
      this.walletStatus = { checked: true, reachable: false, loaded: [] };
    }
  },

  async loadBalances() {
    if (!this.walletStatus.reachable) return;
    try {
      const d = await this.api('/api/wallet/info');
      const mine = d.balances?.mine || {};
      this.wallet.name = d.wallet?.walletname ?? this.wallet.name;
      this.wallet.balance = mine.trusted ?? null;
      this.wallet.pending = mine.untrusted_pending ?? null;
      this.wallet.immature = mine.immature ?? null;
      this.wallet.info = d.wallet || null;
    } catch {
      // No wallet loaded is an expected state, not an error to shout about.
      this.wallet.balance = null;
      this.wallet.pending = null;
      this.wallet.immature = null;
    }
  },

  async loadHistory(count) {
    this.history.busy = true;
    try {
      const n = count || this.history.count;
      const d = await this.api('/api/wallet/history?count=' + n);
      // listtransactions returns oldest-first; show newest-first.
      this.history.txs = (d.transactions || []).slice().reverse();
      this.history.loaded = true;
    } catch {
      this.history.txs = [];
      this.history.loaded = true;
    }
    this.history.busy = false;
  },

  /** Newest few, for the Home card. */
  recentTxs(n = 5) { return this.history.txs.slice(0, n); },

  /** Activity grouped into Today / Yesterday / date buckets. */
  historyGroups() {
    const out = [];
    let label = null;
    for (const tx of this.filteredHistory()) {
      const b = dayBucket(tx.time ?? tx.timereceived);
      if (b !== label) { label = b; out.push({ heading: b }); }
      out.push({ tx });
    }
    return out;
  },

  filteredHistory() {
    const f = this.history.filter;
    if (f === 'all') return this.history.txs;
    if (f === 'in') return this.history.txs.filter((t) => this.txIsIncoming(t.category));
    if (f === 'out') return this.history.txs.filter((t) => t.category === 'send');
    if (f === 'rewards') return this.history.txs.filter(
      (t) => ['stake', 'generate', 'immature'].includes(t.category));
    return this.history.txs;
  },

  async openTx(txid) {
    this.txDetail = { txid, loading: true, data: null };
    try {
      const d = await this.api('/api/wallet/tx/' + txid);
      this.txDetail = { txid, loading: false, data: d.transaction };
    } catch (e) {
      this.txDetail = null;
      this.reportError(e);
    }
  },

  closeTx() { this.txDetail = null; },

  /* --------------------------------------------------------------- receive */

  async newAddress() {
    this.receive.busy = true;
    try {
      const r = await this.api('/api/wallet/receive', {
        method: 'POST',
        body: JSON.stringify({ label: this.receive.label.trim() }),
      });
      this.receive.generated = r;
      this.$nextTick(() => renderQR('addr-qr', r.address, 184));
      this.loadAddressBook();
    } catch (e) { this.reportError(e); }
    this.receive.busy = false;
  },

  resetReceive() {
    this.receive = { label: '', busy: false, generated: null };
  },

  /* --------------------------------------------------------- address book */

  async loadAddressBook() {
    this.book.busy = true;
    try {
      const d = await this.api('/api/wallet/book');
      this.book.addresses = d.addresses || [];
      this.book.loaded = true;
    } catch {
      this.book.addresses = [];
      this.book.loaded = true;
    }
    this.book.busy = false;
  },

  async loadLabels() {
    try {
      const d = await this.api('/api/wallet/labels');
      this.book.labels = (d.labels || []).filter((l) => l && l !== '*');
    } catch { this.book.labels = []; }
  },

  /** Resolve an address to its known label, for Send review. */
  labelFor(address) {
    const hit = (this.book.addresses || []).find((a) => a.address === address);
    return hit?.label || null;
  },

  startRename(entry) {
    this.book.editing = entry.address;
    this.book.draftLabel = entry.label || '';
  },

  cancelRename() { this.book.editing = null; this.book.draftLabel = ''; },

  async saveLabel(address) {
    const label = this.book.draftLabel.trim();
    try {
      await this.api('/api/wallet/label', {
        method: 'POST',
        body: JSON.stringify({ address, label }),
      });
      this.book.editing = null;
      this.showToast(label ? 'Renamed to “' + label + '”' : 'Label removed');
      this.loadAddressBook();
    } catch (e) { this.reportError(e); }
  },

  bookFiltered() {
    const q = this.book.query.trim().toLowerCase();
    let list = this.book.addresses;
    if (q) {
      list = list.filter((a) =>
        (a.label || '').toLowerCase().includes(q) || a.address.toLowerCase().includes(q));
    }
    if (this.book.hideEmpty) list = list.filter((a) => Number(a.amount) > 0);
    const { key, dir } = this.book.sort;
    if (key) {
      const sign = dir === 'desc' ? -1 : 1;
      list = [...list].sort((x, y) => sign * (key === 'amount'
        ? Number(x.amount) - Number(y.amount)
        : (x.label || '\uffff').localeCompare(y.label || '\uffff')));
    }
    return list;
  },

  /** Display-only total of the visible rows (never used for spending math). */
  bookTotal() {
    return this.bookFiltered().reduce((t, a) => t + Number(a.amount || 0), 0).toFixed(9);
  },

  bookSort(key) {
    const s = this.book.sort;
    this.book.sort = (s.key === key)
      ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { key, dir: key === 'amount' ? 'desc' : 'asc' };
  },
  bookArrow(key) {
    const s = this.book.sort;
    return s.key === key ? (s.dir === 'asc' ? '▲' : '▼') : '';
  },
  bookAria(key) {
    const s = this.book.sort;
    return s.key === key ? (s.dir === 'asc' ? 'ascending' : 'descending') : 'none';
  },

  /* ------------------------------------------------- wallet files (manage) */

  async loadWalletManage() {
    this.manage.busy = true;
    try {
      const r = await this.api('/api/wallet/manage');
      this.manage.loaded = r.loaded || [];
      this.manage.onDisk = r.on_disk || [];
      this.manage.persistent = r.persistent_data !== false;
      this.manage.err = '';
    } catch (e) {
      this.manage.err = e.message;
    }
    this.manage.busy = false;
  },

  openCreateWallet() {
    this.go('wallet');
    this.manage.pane = 'create';
    this.manage.create = { name: '', pw: '', pw2: '', reveal: false };
    this.$nextTick(() => document.getElementById('new-wallet-name')?.focus());
  },

  openLoadWallet() {
    this.go('wallet');
    this.manage.pane = 'load';
    this.loadWalletManage();
  },

  walletNameValid() {
    // Mirrors _WALLET_NAME_RE in backend/app/routers/wallet_extra.py
    return /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/.test(this.manage.create.name);
  },

  createWalletReady() {
    const c = this.manage.create;
    return this.walletNameValid() && c.pw.length >= 8 && c.pw === c.pw2;
  },

  async createWallet() {
    const c = this.manage.create;
    if (!this.walletNameValid()) {
      this.showToast('Use letters, numbers, dots, dashes and underscores only', 'danger'); return;
    }
    if (c.pw.length < 8) {
      this.showToast('The passphrase must be at least 8 characters', 'danger'); return;
    }
    if (c.pw !== c.pw2) {
      this.showToast('The passphrases do not match', 'danger'); return;
    }
    this.confirm({
      title: 'Create wallet “' + c.name + '”?',
      body: 'Write your passphrase down somewhere safe first. It encrypts this wallet '
          + 'and it is the only way to spend the coins in it. Nobody — including us — '
          + 'can recover it for you.',
      confirmLabel: 'Create wallet',
      run: async () => {
        this.manage.working = true;
        try {
          await this.api('/api/wallet/manage/create', {
            method: 'POST',
            body: JSON.stringify({ wallet_name: c.name, passphrase: c.pw }),
          });
          this.showToast('Wallet “' + c.name + '” created');
          this.manage.create = { name: '', pw: '', pw2: '', reveal: false };
          this.manage.pane = null;
          await this.loadWalletManage();
          await this.loadWalletStatus();
          await this.loadBalances();
          this.applyStateAttrs();
        } catch (e) { this.reportError(e); }
        this.manage.working = false;
      },
    });
  },

  async loadWalletFile(filename) {
    this.manage.working = true;
    try {
      await this.api('/api/wallet/manage/load', {
        method: 'POST',
        body: JSON.stringify({ filename }),
      });
      this.showToast('Loaded ' + filename);
      this.manage.pane = null;
      this.manage.loadName = '';
      await this.loadWalletManage();
      await this.loadWalletStatus();
      await this.loadBalances();
      this.applyStateAttrs();
    } catch (e) { this.reportError(e); }
    this.manage.working = false;
  },

  unloadWallet(name) {
    this.confirm({
      title: 'Unload ' + (name || 'this wallet') + '?',
      body: 'The wallet file stays on disk and you can load it again at any time. '
          + 'While it is unloaded its balance and history are not visible, and it '
          + 'cannot stake.',
      confirmLabel: 'Unload',
      danger: true,
      run: async () => {
        this.manage.working = true;
        try {
          await this.api('/api/wallet/manage/unload', {
            method: 'POST',
            body: JSON.stringify({ filename: name }),
          });
          this.showToast('Unloaded ' + name);
          await this.loadWalletManage();
          await this.loadWalletStatus();
          this.applyStateAttrs();
        } catch (e) { this.reportError(e); }
        this.manage.working = false;
      },
    });
  },

  async backupWallet() {
    this.manage.backupBusy = true;
    try {
      const r = await this.api('/api/wallet/manage/backup', { method: 'POST' });
      this.manage.backupPath = r.path || '';
      this.manage.backupFile = r.file || '';
      this.markBackedUp();
      this.showToast('Backup saved — downloading…');
      if (this.manage.backupFile) this.downloadBackup(this.manage.backupFile);
    } catch (e) { this.reportError(e); }
    this.manage.backupBusy = false;
  },

  downloadBackup(file) {
    const name = file || this.manage.backupFile;
    if (!name) return;
    const tok = document.cookie.match(/(?:^|; )b3_csrf=([^;]+)/);
    const url = '/api/wallet/manage/backup/download?file=' + encodeURIComponent(name)
      + '&t=' + Date.now();
    fetch(url, { headers: { 'x-csrf-token': tok ? tok[1] : '' } })
      .then((r) => {
        if (!r.ok) throw new Error('download failed');
        return r.blob();
      })
      .then((b) => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(b);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      })
      .catch(() => this.showToast('Could not download backup — it is still saved on the server', 'warning'));
  },

  /* ------------------------------------------------- coin control (UTXOs) */

  async loadUtxos() {
    this.utxos.busy = true;
    try {
      const d = await this.api('/api/wallet/utxos?minconf=' + this.utxos.minconf);
      this.utxos.list = d.utxos || [];
      this.utxos.loaded = true;
    } catch (e) {
      this.utxos.list = [];
      this.utxos.loaded = true;
      this.reportError(e);
    }
    this.utxos.busy = false;
  },

  /** Plain-English summary so the UTXO table is not just a wall of hashes. */
  utxoSummary() {
    const list = this.utxos.list || [];
    const unspendable = list.filter((u) => !u.spendable).length;
    return {
      count: list.length,
      unspendable,
      spendable: list.length - unspendable,
    };
  },

  /* ---------------------------------------------------------- sign/verify */

  async signMessage() {
    if (!isValidAddress(this.signv.address)) {
      this.showToast('Enter one of your own B3 addresses', 'danger'); return;
    }
    this.signv.busy = true; this.signv.sig = '';
    try {
      const d = await this.api('/api/wallet/signmessage', {
        method: 'POST',
        body: JSON.stringify({ address: this.signv.address.trim(), message: this.signv.message }),
      });
      this.signv.sig = d.signature;
      this.showToast('Message signed');
    } catch (e) { this.reportError(e); }
    this.signv.busy = false;
  },

  async verifyMessage() {
    this.signv.vbusy = true; this.signv.verified = null;
    try {
      const d = await this.api('/api/wallet/verifymessage', {
        method: 'POST',
        body: JSON.stringify({
          address: this.signv.vaddress.trim(),
          signature: this.signv.vsig.trim(),
          message: this.signv.vmessage,
        }),
      });
      this.signv.verified = !!d.valid;
    } catch (e) { this.reportError(e); }
    this.signv.vbusy = false;
  },
};
