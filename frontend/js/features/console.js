/* B3 Hive - Expert console (Advanced mode).

User-controlled trust model (v0.4.6): the browser NEVER talks to the
node - it POSTs a command string to /api/console/run and the backend
executes it. What the backend runs depends on the client's trust
context, which the OPERATOR controls:

- localhost / allowlisted networks / 2FA remotes (operator opt-in):
  FULL access. QT parity - everything runs. Danger commands (spends,
  unlock, key material) get an honest confirm dialog, never a wall.
- operator restricted remotes: read-only mode with a clear notice.

Trust settings (networks, remote policy) are editable from the console
view by any full-trust client. The platform warns about risks; the
user accepts the tradeoff - it's their coins. */

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
    return (c.catalog.runnable || [])
      .filter((m) => m.startsWith(q))
      .slice(0, 8);
  },

  consoleAccept(s) {
    this.console.input = s + ' ';
    document.getElementById('console-input').focus();
  },

  async consoleLoad() {
    if (this.console.loaded || this.console.gate !== null) return;
    try {
      const d = await this.api('/api/console/catalog');
      this.console.catalog = d;
      this.console.mode = d.mode;
      this.console.settings = {
        networks: d.networks, remote_full_access: d.remote_full_access,
      };
      this.console.sform = {
        networks: d.networks || '', remote_full_access: !!d.remote_full_access,
      };
      this.console.loaded = true;
      this.console.gate = false;
    } catch (e) {
      // 403 from the trust gate: show the remediation card, not a shell.
      this.console.gate = true;
      // The backend detail is operator-facing remediation; prefer it over
      // the generic 403 text from friendlyError().
      this.console.gateMsg = (e && e.data && typeof e.data.detail === 'string')
        ? e.data.detail : (e && e.message) || '';
    }
  },

  consoleDanger(cmd) {
    const c = this.console;
    const method = cmd.split(/\s+/)[0];
    return (c.catalog && c.catalog.danger && c.catalog.danger[method]) || null;
  },

  async consoleSubmit() {
    const c = this.console;
    const raw = (c.input || '').trim();
    if (!raw || c.busy) return;

    // Full mode: danger commands run - after an honest warning.
    // The platform's job is the warning + audit log, not a prohibition.
    if (c.mode === 'full' && c.catalog && c.catalog.danger) {
      const warn = this.consoleDanger(raw);
      if (warn) {
        this.confirm({
          title: 'Run ' + raw.split(/\s+/)[0] + '?',
          body: warn,
          detail: ['Everything you run here is audit-logged. This command '
            + 'reaches the node exactly as typed.'],
          confirmLabel: 'Run it',
          danger: true,
          run: () => this.consoleExec(raw),
        });
        return;
      }
    }
    await this.consoleExec(raw);
  },

  async consoleExec(raw) {
    const c = this.console;
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
      // Node errors, guidance, rate limits: plain language from the
      // backend - show the detail verbatim when present.
      const msg = (e && e.data && typeof e.data.detail === 'string')
        ? e.data.detail : (e && e.message) || 'request failed';
      c.lines.push({ kind: 'err', text: msg });
    } finally {
      c.busy = false;
      if (c.lines.length > 200) c.lines.splice(0, c.lines.length - 200);
      this.$nextTick(() => {
        const el = document.getElementById('console-scroll');
        if (el) el.scrollTop = el.scrollHeight;
        document.getElementById('console-input').focus();
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
    navigator.clipboard.writeText(text);
    this.showToast('Console output copied');
  },

  async consoleSaveSettings() {
    const c = this.console;
    if (!c.sform || c.sbusy) return;
    c.sbusy = true;
    try {
      const d = await this.api('/api/console/settings', {
        method: 'PUT',
        body: JSON.stringify({
          networks: (c.sform.networks || '').trim(),
          remote_full_access: !!c.sform.remote_full_access,
        }),
      });
      c.settings = { networks: d.networks, remote_full_access: d.remote_full_access };
      this.showToast('Console trust settings saved');
    } catch (e) {
      const msg = (e && e.data && typeof e.data.detail === 'string')
        ? e.data.detail : (e && e.message) || 'saving failed';
      this.showToast(msg, 'error');
    } finally {
      c.sbusy = false;
    }
  },

};
