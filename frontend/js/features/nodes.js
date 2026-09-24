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

  /* ------------------------------------------- registry management (Fleet view) */

  async submitAddNode() {
    const f = this.nodes.form;
    if (!f.name.trim()) { this.showToast('Give the node a name', 'error'); return; }
    const port = Number(f.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      this.showToast('Port must be between 1 and 65535', 'error'); return;
    }
    this.nodes.busy = true;
    try {
      // The backend checks the node answers RPC with these credentials
      // before saving, so a typo fails here rather than later in the fleet.
      await this.api('/api/nodes', { method: 'POST', body: JSON.stringify({
        name: f.name.trim(), host: f.host.trim() || '127.0.0.1', port,
        rpc_user: f.rpc_user, rpc_password: f.rpc_password,
      }) });
      this.nodes.form = { name: '', host: '', port: 38647, rpc_user: '', rpc_password: '' };
      this.nodes.addOpen = false;
      this.showToast('Node added');
      await Promise.all([this.loadNodes(), this.loadFleet()]);
    } catch (e) { this.reportError(e); }
    this.nodes.busy = false;
  },

  askRemoveNode(row) {
    this.confirm({
      title: 'Remove ' + row.name + '?',
      body: 'This only forgets the connection here — the node itself, its wallet and '
          + 'its coins are not touched. You can add it again later.',
      detail: [{ key: 'Node', value: row.name }],
      confirmLabel: 'Remove node',
      danger: true,
      run: async () => {
        try {
          await this.api('/api/nodes/' + row.id, { method: 'DELETE' });
          if (String(this.nodes.selected) === String(row.id)) this.nodes.selected = '';
          this.showToast(row.name + ' removed');
          await Promise.all([this.loadNodes(), this.loadFleet()]);
        } catch (e) { this.reportError(e); }
      },
    });
  },

  askDefaultNode(row) {
    this.confirm({
      title: 'Make ' + row.name + ' the default?',
      body: 'Every screen uses the default node unless you pick another one in the node '
          + 'switcher — including wallet actions like sending and staking.',
      detail: [{ key: 'Node', value: row.name }],
      confirmLabel: 'Make default',
      run: async () => {
        try {
          await this.api('/api/nodes/' + row.id + '/default', { method: 'POST' });
          this.showToast(row.name + ' is now the default node');
          await Promise.all([this.loadNodes(), this.loadFleet()]);
        } catch (e) { this.reportError(e); }
      },
    });
  },

  startPin(row) {
    this.nodes.pinId = row.id;
    this.nodes.pinValue = row.daemon_build || '';
  },

  async savePin(row) {
    try {
      await this.api('/api/nodes/' + row.id, {
        method: 'PUT', body: JSON.stringify({ daemon_build: this.nodes.pinValue.trim() }),
      });
      this.nodes.pinId = null;
      this.showToast(this.nodes.pinValue.trim() ? 'Build pinned' : 'Pin cleared');
      await this.loadFleet();
    } catch (e) { this.reportError(e); }
  },

  /* ---------------------------------------------- dev build (from source) */

  askInstallDevBuild() {
    const d = this.devbuild;
    const url = d.url.trim(), sha = d.sha256.trim().toLowerCase(), label = d.label.trim();
    if (!/^https?:\/\//.test(url)) { this.showToast('The URL must start with http:// or https://', 'error'); return; }
    if (!/^[0-9a-f]{64}$/.test(sha)) { this.showToast('SHA-256 must be 64 hex characters', 'error'); return; }
    if (!label) { this.showToast('Give the build a label, e.g. flowmesh-abc123', 'error'); return; }
    this.confirm({
      title: 'Install build ' + label + ' on this container?',
      body: 'The wallet is backed up first and the install refuses to continue if that '
          + 'backup fails. Then the node stops, the build is downloaded and checked '
          + 'against the SHA-256, swapped in, and the node restarts — expect a few '
          + 'minutes offline. An unreleased build can write a wallet an official '
          + 'release cannot read.',
      detail: [
        { key: 'Label', value: label },
        { key: 'From', value: url, mono: true },
        { key: 'SHA-256', value: sha, mono: true },
      ],
      confirmLabel: 'Install build',
      danger: true,
      run: async () => {
        d.busy = true;
        try {
          await this.api('/api/system/upgrade', {
            method: 'POST', body: JSON.stringify({ url, sha256: sha, tag: label }),
          });
          this.devbuild = { url: '', sha256: '', label: '', busy: false };
          this.showToast('Install queued — follow it in Node → Logs');
        } catch (e) { this.reportError(e); }
        d.busy = false;
      },
    });
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
    // Phase 4.2: the pin, and — loudly — a confirmed drift from it.
    // build_mismatch === null means "pin is a dev-build label, not
    // verifiable from RPC alone" (see backend routers/nodes.py
    // _build_mismatch); that case shows the pin but no claim either way.
    if (row.daemon_build) {
      parts.push(row.build_mismatch
        ? '⚠ pinned ' + row.daemon_build + ' but running ' + (row.subversion || 'something else')
        : 'pinned ' + row.daemon_build);
    }
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
    if (row.build_mismatch) return 'badge-danger';
    if (row.monitor && row.monitor.stall_count > 0) return 'badge-warning';
    if (row.staking && row.staking.running) return 'badge-success';
    return 'badge-neutral';
  },

  fleetBadgeText(row) {
    if (!row.reachable) return 'Unreachable';
    // A build drift from the pin outranks a stall/staking badge — a
    // validator running the wrong binary is the bigger risk to a
    // FlowMesh test's results (docs/MULTINODE_PLAN.md Phase 4.2).
    if (row.build_mismatch) return 'Wrong build';
    if (row.monitor && row.monitor.stall_count > 0) return 'Stalled';
    if (row.staking && row.staking.running) return 'Staking';
    return 'Online';
  },
};
