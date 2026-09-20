/* B3 Hive — session: login, 2FA, lock state, theme and mode.

   Auth flow (backend/app/routers/auth.py):
     POST /login  -> {totp_required}
     if totp_required: POST /2fa {code}
   Localhost skips TOTP when LOCALHOST_SKIP_2FA is on (the default), so the
   UI must not assume a 2FA step exists.

   Lock state is deliberately separate from "is a wallet loaded" — see
   core/state.js walletState(). */

import { fmtClock } from '../core/format.js';
import { seal } from '../core/envelope.js';

export const sessionMixin = {

  fmtClock,


  /* --------------------------------------------------------------- login -- */

  async login() {
    this.loginErr = ''; this.loginBusy = true;
    try {
// ECDH-seal the password when envelope encryption is available
// (v0.5.0 transport security): the cleartext never crosses the wire.
const env = await seal(this.loginPw, 'b3hive-login');
      const r = await this.api('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify(env ? { env } : { password: this.loginPw }),
      });
      if (r.totp_required) {
        this.loginStep2 = true;
      } else {
        this.session.authenticated = true;
        this.loginPw = '';
        await this.afterAuth();
      }
    } catch (e) {
      this.loginErr = e.message;
    }
    this.loginBusy = false;
  },

  async verify2fa() {
    this.loginErr = ''; this.loginBusy = true;
    try {
      await this.api('/api/auth/2fa', {
        method: 'POST',
        body: JSON.stringify({ code: this.totpCode.trim() }),
      });
      this.session.authenticated = true;
      this.loginStep2 = false;
      this.loginPw = ''; this.totpCode = '';
      await this.afterAuth();
    } catch (e) {
      this.loginErr = e.message;
      this.totpCode = '';
    }
    this.loginBusy = false;
  },

  async logout() {
    try { await this.api('/api/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
    this.session = { authenticated: false, wallet_unlocked: false, totp_configured: false };
    this.loginPw = ''; this.totpCode = ''; this.loginStep2 = false;
    this.stopPolling();
  },

  async checkSession() {
    try {
      const s = await this.api('/api/auth/status');
      this.session = s;
      if (s.authenticated) await this.afterAuth();
    } catch {
      this.session.authenticated = false;
    } finally {
      this.booted = true;
    }
  },

  async refreshSession() {
    try {
      const was = this.session.wallet_unlocked;
      this.session = await this.api('/api/auth/status');
      if (was && !this.session.wallet_unlocked) this.unlockLeft = 0;
    } catch { /* best effort */ }
  },

  /** Runs once a real session exists: decide wizard vs app, then start polling. */
  async afterAuth() {
    await this.checkSetup();
    // checkSetup() populates walletStatus, so mirror it onto <html> now —
    // otherwise data-wallet stays stale until the first poll 15s later.
    this.applyStateAttrs();
    if (this.setupActive()) {
      this.loadWizard();
      return;
    }
    this.initRouter();
    this._applyHash?.();
    this.onEnterView(this.view);
    this.startPolling();
    await this.pollNow();
  },

  /* ---------------------------------------------------------- lock state -- */

  /** Explicit unlock, outside any 423 intercept (Wallet view, palette). */
  askUnlock() {
    this.unlockPrompt = {
      show: true, pw: '', busy: false, err: '', reveal: false, retry: null,
    };
  },

  async lockWallet() {
    try {
      await this.api('/api/wallet/lock', { method: 'POST' });
      this.session.wallet_unlocked = false;
      this.unlockLeft = 0;
      this.showToast('Wallet locked');
      this.refreshSession();
    } catch (e) { this.reportError(e); }
  },

  /** Live countdown in the wallet badge, so "unlocked" is never a mystery
   *  about how long. unlocked_for_s comes from POST /api/wallet/unlock. */
  startUnlockCountdown(seconds) {
    this.unlockLeft = Math.max(0, Number(seconds) || 0);
    clearInterval(this._unlockTimer);
    if (!this.unlockLeft) return;
    this._unlockTimer = setInterval(() => {
      this.unlockLeft -= 1;
      if (this.unlockLeft <= 0) {
        clearInterval(this._unlockTimer);
        this.unlockLeft = 0;
        this.session.wallet_unlocked = false;
        this.refreshSession();
      }
    }, 1000);
  },

  /* ------------------------------------------------------------ 2FA setup -- */

  async setupTotp() {
    this.totpBusy = true;
    try {
      this.totpSetup = await this.api('/api/auth/totp/setup', { method: 'POST' });
      this.$nextTick(() => this.renderQR('totp-qr', this.totpSetup.provisioning_uri, 192));
    } catch (e) { this.reportError(e); }
    this.totpBusy = false;
  },

  /* ---------------------------------------------------- passphrase change -- */

  async changePassphrase() {
    if (this.pwchange.next !== this.pwchange.repeat) {
      this.showToast('The new passphrases do not match', 'danger'); return;
    }
    if (this.pwchange.next.length < 8) {
      this.showToast('The new passphrase must be at least 8 characters', 'danger'); return;
    }
    // Irreversible-ish and security-relevant: confirm in a modal, never
    // window.confirm (banned by brief §7).
    this.confirm({
      title: 'Change your wallet passphrase?',
      body: 'Your wallet will re-lock immediately and every future unlock will need '
          + 'the new passphrase. If you forget it, your coins cannot be recovered — '
          + 'not by us, not by anyone. Make sure your backup and your password manager '
          + 'are up to date first.',
      confirmLabel: 'Change passphrase',
      danger: true,
      run: async () => {
        this.pwchange.busy = true;
        try {
          await this.api('/api/wallet/passphrase-change', {
            method: 'POST',
            body: JSON.stringify({
              old_passphrase: this.pwchange.current,
              new_passphrase: this.pwchange.next,
            }),
          });
          this.pwchange = { current: '', next: '', repeat: '', busy: false };
          this.session.wallet_unlocked = false;
          this.unlockLeft = 0;
          await this.refreshSession();
          this.showToast('Passphrase changed — unlock again when you next need it');
        } catch (e) { this.reportError(e); }
        this.pwchange.busy = false;
      },
    });
  },

  /* ------------------------------------------------------ theme and mode -- */

  setTheme(t) {
    this.theme = t === 'light' ? 'light' : 'dark';
    localStorage.setItem('b3-theme', this.theme);
    document.documentElement.setAttribute('data-theme', this.theme);
    document.querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', this.theme === 'light' ? '#faf9f6' : '#12100e');
  },
  toggleTheme() { this.setTheme(this.theme === 'dark' ? 'light' : 'dark'); },

  setMode(m) {
    this.mode = m === 'advanced' ? 'advanced' : 'simple';
    localStorage.setItem('b3-mode', this.mode);
    document.documentElement.setAttribute('data-mode', this.mode);
  },
  toggleMode() {
    const next = this.mode === 'simple' ? 'advanced' : 'simple';
    this.setMode(next);
    this.showToast(next === 'advanced'
      ? 'Advanced mode on — coin control, batch actions and node logs are now available'
      : 'Back to Simple mode');
    // Leaving Advanced while on an Advanced-only view would strand the user.
    if (next === 'simple' && this.currentView().advanced) this.go('home');
  },

  /* ------------------------------------------------------------- polling -- */

  startPolling() {
    this.stopPolling();
    this._poll = setInterval(() => this.pollNow(), 15000);
    this._alertPoll = setInterval(() => this.loadAlerts(), 30000);
  },

  stopPolling() {
    clearInterval(this._poll); this._poll = null;
    clearInterval(this._alertPoll); this._alertPoll = null;
  },

  async pollNow() {
    if (!this.session.authenticated || this.setupActive()) return;
    await Promise.allSettled([
      this.refreshSession(),
      this.loadChain(),
      this.loadWalletStatus(),
      this.loadBalances(),
      this.loadStakingSnapshot(),
      this.loadAlerts(),
      this.checkSetup(),
    ]);
    this.applyStateAttrs();
  },
};
