/* B3 Hive — Tailscale remote access (v0.7.0).

 The container bundles tailscaled; these methods drive the backend's
 /api/tailscale/* endpoints. Two jobs:
 - honest status (joined? serving HTTPS?) shown in Settings,
 - guided join with an auth key (the key is sent once and never stored
 by the browser; it is not logged by the backend either).

 The ts.net URL is real HTTPS (Let's Encrypt), which also makes the
 browser a secure context — envelope encryption then always seals. */

export const tailscaleMixin = {

  async loadTailscale() {
    if (!this.session.authenticated) return;
    try {
      const st = await this.api('/api/tailscale/status');
      this.ts.loaded = true;
      this.ts.available = st.available;
      this.ts.joined = st.joined;
      this.ts.https_url = st.https_url;
      this.ts.serve_enabled = st.serve_enabled;
      this.ts.tailnet = st.tailnet;
      this.ts.detail = st.detail || '';
    } catch (e) {
      // Backend predates the router or tailscale missing: stay neutral.
      this.ts.loaded = false;
    }
  },

  async tsJoin() {
    const key = (this.ts.authkey || '').trim();
    if (!key) { this.showToast('Paste your Tailscale auth key first.', 'warning'); return; }
    this.ts.busy = true; this.ts.error = '';
    try {
      await this.api('/api/tailscale/join', {
        method: 'POST',
        body: JSON.stringify({ authkey: key }),
      });
      this.ts.authkey = ''; // never keep the key around
      this.showToast('Joined your tailnet.', 'success');
      await this.loadTailscale();
      // Joining enables the node's ts.net name; offer HTTPS serving next.
      this.ts.joinOpen = false;
    } catch (e) {
      this.ts.error = e.message;
    } finally {
      this.ts.busy = false;
    }
  },

  async tsServe(enable) {
    this.ts.busy = true; this.ts.error = '';
    try {
      await this.api('/api/tailscale/serve', {
        method: 'POST',
        body: JSON.stringify({ enable }),
      });
      this.showToast(enable ? 'HTTPS enabled — see your ts.net address.'
                        : 'HTTPS serving turned off.', 'success');
      await this.loadTailscale();
    } catch (e) {
      this.ts.error = e.message;
    } finally {
      this.ts.busy = false;
    }
  },

  async tsLeave() {
    this.confirm({
      title: 'Disconnect Tailscale?',
      body: 'Remote access through your tailnet stops immediately. '
          + 'Your identity is kept, so you can rejoin later with a new auth key.',
      confirmLabel: 'Disconnect',
      run: async () => {
        this.ts.busy = true; this.ts.error = '';
        try {
          await this.api('/api/tailscale/leave', { method: 'POST' });
          this.showToast('Left the tailnet.', 'success');
          await this.loadTailscale();
        } catch (e) {
          this.ts.error = e.message;
        } finally {
          this.ts.busy = false;
        }
      },
    });
  },
};