/* B3 Hive — Expert console (Advanced mode, trusted networks only).

 A QT-style command surface, minus the parts a web wallet must never
 expose. The backend enforces three tiers:
   read    -> always runnable while the node is up (locked wallet fine)
   unlock  -> needs the wallet unlocked; api() intercepts the 423 and
              shows the point-of-action passphrase prompt, then retries
   blocked -> wallet-affecting or dangerous; guidance only, never run

 The IP gate means a remote session sees a 403 with remediation, not a
 login-loop. Autocomplete comes from the backend catalog, which mirrors
 the RPC allowlist — the browser never invents a command list.

 SECURITY NOTES (mirrored from backend/app/routers/console.py):
 - Every run is audit-logged; results are truncated before display.
 - Rate-limited to 30 runs/min server-side; the UI shows the 429 honestly. */

export const consoleMixin = {

  consoleEnter(e) {
    if (e.key === 'Enter') { e.preventDefault(); this.consoleSubmit(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); this.consoleHistory(-1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); this.consoleHistory(1); }
  },

  consoleHistory(dir) {
    const c = this.console;
    if (!c.history.length) return;
    c.histPos = Math.min(Math.max(c.histPos + dir, -1), c.history.length - 1);
    c.input = c.histPos === -1 ? '' : c.history[c.histPos];
  },

  /** Autocomplete candidates for the current input (method names only). */
  consoleSuggestions() {
    const c = this.console;
    if (!c.catalog || !c.input || /\s/.test(c.input)) return [];
    const q = c.input.toLowerCase();
    return c.catalog.read
      .concat(c.catalog.unlock)
      .filter((m) => m.startsWith(q))
      .slice(0, 8);
  },

  consoleAccept(s) {
    this.console.input = s + ' ';
    document.getElementById('console-input')?.focus();
  },

  async consoleLoad() {
    if (this.console.loaded || this.console.gate !== null) return;
    try {
      const d = await this.api('/api/console/catalog');
      this.console.catalog = d;
      this.console.loaded = true;
      this.console.gate = false;
    } catch (e) {
      // 403 from the IP gate: show the remediation card instead of a shell.
      this.console.gate = true;
      // The backend detail is operator-facing remediation; prefer it over
      // the generic 403 text from friendlyError().
      this.console.gateMsg = (e && e.data && typeof e.data.detail === 'string')
        ? e.data.detail : (e && e.message) || '';
    }
  },

  async consoleSubmit() {
    const c = this.console;
    const raw = (c.input || '').trim();
    if (!raw || c.busy) return;
    c.busy = true;
    c.input = '';
    c.histPos = -1;
    if (c.history[c.history.length - 1] !== raw) c.history.push(raw);
    c.lines.push({ kind: 'in', text: raw });
    try {
      const d = await this.api('/api/console/run', {
        method: 'POST', body: JSON.stringify({ command: raw }),
      });
      c.lines.push({ kind: 'out', text: JSON.stringify(d.result, null, 2) });
    } catch (e) {
      // Tier guidance and RPC errors are plain language from the backend:
      // show the detail verbatim when present, else the friendly message.
      const raw = (e && e.data && typeof e.data.detail === 'string')
        ? e.data.detail : (e && e.message) || 'request failed';
      c.lines.push({ kind: 'err', text: raw });
    } finally {
      c.busy = false;
      if (c.lines.length > 200) c.lines.splice(0, c.lines.length - 200);
      this.$nextTick(() => {
        const el = document.getElementById('console-scroll');
        if (el) el.scrollTop = el.scrollHeight;
        document.getElementById('console-input')?.focus();
      });
    }
  },

  consoleClear() {
    this.console.lines = [];
  },

  consoleCopy() {
    const text = this.console.lines
      .map((l) => (l.kind === 'in' ? '> ' + l.text : l.text))
      .join('\n');
    navigator.clipboard?.writeText(text);
    this.showToast('Console output copied');
  },
};
