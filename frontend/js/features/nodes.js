/* B3 Hive — node registry (multi-node fleet console, Phase 1).

   The X-B3-Node header is injected by api() (core/api.js) from
   `nodes.selected`; no selection means the backend's default node, so this
   mixin never has to special-case "no selection" beyond that one field. */

export const nodesMixin = {

  async loadNodes() {
    try {
      const r = await this.api('/api/nodes');
      this.nodes.list = r.nodes;
      this.nodes.loaded = true;
    } catch (e) { this.reportError(e); }
  },

  selectNode(id) {
    // '' -> the default node (no X-B3-Node header sent at all).
    this.nodes.selected = id ? String(id) : '';
  },

  nodeName(id) {
    const n = this.nodes.list.find((x) => String(x.id) === String(id));
    return n ? n.name : null;
  },

  /** The node a wallet-affecting action would run against right now, for
   *  confirm dialogs (docs/MULTINODE_PLAN.md 2.3 — name the node in every
   *  such confirmation, not just in a switcher elsewhere on the page).
   *  Sending from the wrong validator's wallet is the failure mode that
   *  costs real coins, so this reads the SAME selection api() uses. */
  currentNodeLabel() {
    if (this.nodes.selected) {
      return this.nodeName(this.nodes.selected) || 'selected node';
    }
    const def = this.nodes.list.find((n) => n.is_default);
    return def ? def.name + ' (default)' : 'default node';
  },

  async addNode(form) {
    const body = {
      name: form.name, host: form.host || '127.0.0.1', port: Number(form.port),
      rpc_user: form.rpc_user || '', rpc_password: form.rpc_password || '',
    };
    const d = await this.api('/api/nodes', { method: 'POST', body: JSON.stringify(body) });
    await this.loadNodes();
    return d;
  },

  async removeNode(id) {
    await this.api('/api/nodes/' + id, { method: 'DELETE' });
    if (String(this.nodes.selected) === String(id)) this.nodes.selected = '';
    await this.loadNodes();
  },

  async setDefaultNode(id) {
    await this.api('/api/nodes/' + id + '/default', { method: 'POST' });
    await this.loadNodes();
  },

  /* --------------------------------------------------------- fleet view */

  async loadFleet() {
    this.fleet.busy = true;
    try {
      const r = await this.api('/api/nodes/fleet');
      this.fleet.rows = r.nodes;
      this.fleet.top_height = r.top_height;
      this.fleet.loaded = true;
    } catch (e) { this.reportError(e); }
    this.fleet.busy = false;
  },

  /** One compact sentence of everything the fleet row has for this node —
   *  keeps the markup a single x-text instead of a wall of nested x-show. */
  fleetRowMeta(row) {
    if (!row.reachable) return row.error || 'Unreachable';
    const parts = [];
    if (row.blocks != null) {
      parts.push(this.fmtInt(row.blocks) + ' blocks'
        + (row.height_delta ? ' (−' + this.fmtInt(row.height_delta) + ')' : ''));
    }
    if (row.peers != null) parts.push(this.fmtInt(row.peers) + ' peers');
    if (row.staking) {
      parts.push('Staking ' + (row.staking.running ? 'running' : 'stopped')
        + (row.staking.blocks_produced != null
          ? ' (' + this.fmtInt(row.staking.blocks_produced) + ' produced)' : ''));
    }
    if (row.finality) {
      parts.push((row.finality.bound ? 'Bound' : 'Not bound')
        + (row.finality.member ? ', member' : '')
        + (row.finality.weight != null && row.finality.total_weight != null
          ? ' (' + this.fmtInt(row.finality.weight) + '/' + this.fmtInt(row.finality.total_weight) + ')'
          : ''));
    }
    if (row.fn_balance != null) parts.push('FN ' + this.fmtInt(row.fn_balance));
    // Background monitor status (Phase 3): a stall the monitor already
    // caught is worth surfacing here even though the node still answers
    // RPC right now — that's exactly the gap between "reachable" and
    // "healthy".
    if (row.monitor && row.monitor.stall_count > 0) {
      parts.push('⚠ stalled ' + this.fmtInt(row.monitor.stall_count) + '×');
    }
    return parts.join(' · ') || 'No wallet loaded on this node';
  },

  fleetBadgeClass(row) {
    if (!row.reachable) return 'badge-danger';
    if (row.monitor && row.monitor.stall_count > 0) return 'badge-warning';
    if (row.staking && row.staking.running) return 'badge-success';
    return 'badge-neutral';
  },

  fleetBadgeText(row) {
    if (!row.reachable) return 'Unreachable';
    if (row.monitor && row.monitor.stall_count > 0) return 'Stalled';
    if (row.staking && row.staking.running) return 'Staking';
    return 'Online';
  },
};
