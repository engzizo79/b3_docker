/* B3 Hive — the Node view: health, peers, configuration, logs, recovery.

   Two things the previous UI never wired at all are surfaced here:
   GET /api/chain/bridge and GET /api/chain/supply.

   Raw RPC payloads are presented as humanised DetailList rows, not
   JSON.stringify dumps — the same data, without reading like developer
   output. `Copy raw` is still one click away for operators. */

import { toDetailRows, fmtInt, fmtAmount, fmtBytes } from '../core/format.js';

export const nodeMixin = {

  toDetailRows,

  /* ---------------------------------------------------------- core polls -- */

  async loadChain() {
    try {
      const d = await this.api('/api/chain/summary');
      this.chain.blocks = d.blockchain?.blocks ?? null;
      this.chain.headers = d.blockchain?.headers ?? null;
      this.chain.connections = d.network?.connections ?? null;
      this.chain.mempool = d.mempool?.size ?? null;
      this.chain.summary = d;
      this.chain.sync = d.sync || null;
      // The node answered: end the honest "starting" state.
      if (this.node.starting || this.nodeStarting()) {
        this.node.starting = false;
        this.showToast('Your node is responding');
      }
    } catch {
      // Expected while the node boots (~5 min index scan). nodeDown() reads
      // chain.blocks === null, which drives the guided "node is starting" card.
      this.chain.blocks = null;
      this.chain.sync = null;
      this.chain.summary = null;
    }
    try {
      const f = await this.api('/api/chain/finality');
      this.chain.finality = f.finality || null;
    } catch { this.chain.finality = null; }
  },

  /** Heavier reads, only when the Node view is open. */
  async loadNodeExtras() {
    this.node.busy = true;
    await Promise.allSettled([
      this.loadPeers(),
      (async () => {
        try { this.node.bridge = (await this.api('/api/chain/bridge')).bridge || null; }
        catch { this.node.bridge = null; }
      })(),
      (async () => {
        try { this.node.supply = (await this.api('/api/chain/supply')).supply || null; }
        catch { this.node.supply = null; }
      })(),
      (async () => {
        try { this.node.conf = await this.api('/api/setup/conf'); }
        catch { this.node.conf = null; }
      })(),
    ]);
    this.node.busy = false;
  },

  async loadPeers() {
    try {
      const d = await this.api('/api/chain/peers');
      this.node.peers = d.peers || [];
    } catch { this.node.peers = []; }
  },

  async loadSystemInfo() {
    try { this.node.info = await this.api('/api/system/info'); }
    catch { this.node.info = null; }
  },

  /* ---------------------------------------------------------- presentation */

  peerSummary() {
    const p = this.node.peers || [];
    const inbound = p.filter((x) => x.inbound).length;
    return { total: p.length, inbound, outbound: p.length - inbound };
  },

  peerPing(p) {
    return p.ping != null ? Math.round(p.ping * 1000) + ' ms' : '—';
  },

  /** Finality in one sentence rather than a blob of BLS fields. */
  finalityHeadline() {
    const f = this.chain.finality;
    if (!f) return { text: 'Not available yet', cls: 'badge-neutral', word: 'Unknown' };
    if (f.active === true) {
      return { text: 'BLS checkpoints are being finalised.', cls: 'badge-success', word: 'Active' };
    }
    return {
      text: 'Modern proof-of-stake finality has not activated on this chain yet.',
      cls: 'badge-info', word: 'Not active',
    };
  },

  supplyRows() {
    const s = this.node.supply;
    if (!s) return [];
    return [
      { label: 'Total supply', value: fmtAmount(s.total_amount, { unit: true, maxDecimals: 0 }) },
      { label: 'Unspent outputs', value: fmtInt(s.txouts) },
      { label: 'At block', value: fmtInt(s.height ?? s.bestblock) },
      { label: 'Database size', value: fmtBytes(s.disk_size) },
    ].filter((r) => r.value && r.value !== '—');
  },

  /* ------------------------------------------------- node configuration -- */

  editConf() {
    this.node.confForm = { ...(this.node.conf?.editable || {}) };
    this.node.confEditing = true;
  },

  cancelConf() { this.node.confEditing = false; },

  applyConf() {
    this.confirm({
      title: 'Apply node configuration?',
      body: 'The values are written to b3coin.conf. Your node needs a restart before '
          + 'they take effect, which briefly interrupts staking and pauses the wallet.',
      confirmLabel: 'Apply and restart',
      run: async () => {
        this.node.working = true;
        try {
          await this.api('/api/setup/conf/apply', {
            method: 'POST',
            body: JSON.stringify({ conf: this.node.confForm }),
          });
          this.node.confEditing = false;
          await this.restartNodeNow();
          this.showToast('Configuration applied — your node is restarting');
          this.loadNodeExtras();
        } catch (e) { this.reportError(e); }
        this.node.working = false;
      },
    });
  },

  /* -------------------------------------------------------- node control -- */

  restartNode() {
    this.confirm({
      title: 'Restart your node?',
      body: 'The node stops and starts again. Staking pauses and balances are '
          + 'unavailable for a few minutes while it re-scans the block index. '
          + 'No coins are at risk.',
      confirmLabel: 'Restart node',
      danger: true,
      run: () => this.restartNodeNow(),
    });
  },

  async restartNodeNow() {
    try {
      await this.api('/api/setup/restart-node', { method: 'POST' });
      this.showToast('Restart queued — this takes a few minutes');
    } catch (e) { this.reportError(e); }
  },

  async startNode() {
    this.node.working = true;
    try {
      await this.api('/api/setup/start-node', { method: 'POST' });
      // Mirror the backend flag immediately: the entrypoint removes the
      // deferred marker when it launches the daemon, so from now on
      // daemon_deferred=false + node-not-answering means "starting" —
      // nodeStarting() keeps reporting true across reloads until it answers.
      this.setup.daemon_deferred = false;
      this.node.starting = true;
      this.showToast('Starting your node — this takes a few minutes');
      setTimeout(() => this.pollNow(), 3000);
    } catch (e) { this.reportError(e); }
    this.node.working = false;
  },

  /* ---------------------------------------------------------------- logs -- */

  async loadLogs() {
    this.node.logBusy = true;
    this.node.logNote = '';
    try {
      const r = await this.api('/api/system/logs?lines=' + this.node.logCount);
      this.node.logs = r.lines || [];
      this.node.logNote = r.note || '';
    } catch (e) {
      this.node.logs = [];
      this.node.logNote = e.message;
    }
    this.node.logBusy = false;
  },

  filteredLogs() {
    const f = this.node.logFilter;
    if (f === 'all') return this.node.logs;
    const pattern = f === 'error' ? /error/i : /warn/i;
    return this.node.logs.filter((l) => pattern.test(l));
  },

  logClass(line) {
    if (/error/i.test(line)) return 'logline err';
    if (/warn/i.test(line)) return 'logline warn';
    return 'logline';
  },

  /* ------------------------------------------------------- raw detail ----- */

  /** Copy the underlying payload for an operator who genuinely wants JSON,
   *  without making JSON the default presentation anywhere. */
  copyRaw(obj, label = 'Raw data copied') {
    this.copyText(JSON.stringify(obj ?? {}, null, 2), label);
  },
};
