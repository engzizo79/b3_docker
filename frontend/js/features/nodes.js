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
};
