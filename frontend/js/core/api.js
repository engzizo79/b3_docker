/* B3 Hive — the single HTTP choke point.

   Ported from the previous app.js `api()` / `promptUnlockRetry()` pair. These
   are load-bearing and deliberately NOT restructured:

   - CSRF double-submit: read the JS-readable `b3_csrf` cookie and mirror it
     into the `x-csrf-token` header on every request.
   - 401 -> drop the session and let the shell fall back to the login screen.
   - 423 -> the wallet is locked. Rather than failing the action, raise the
     point-of-action passphrase modal (QT-style), unlock, and transparently
     retry the ORIGINAL request. `opts._retried` is the infinite-loop guard:
     a retry that 423s again propagates the error instead of re-prompting.

   The browser never talks to the node; every call here is to /api/*. */

import { seal } from './envelope.js';

export const apiMixin = {

  async api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({}, opts.headers || {});

    const m = document.cookie.match(/(?:^|; )b3_csrf=([^;]+)/);
    if (m) headers['x-csrf-token'] = m[1];
    if (opts.body) headers['Content-Type'] = 'application/json';

    let res;
    try {
      res = await fetch(path, {
        method: opts.method || 'GET',
        headers,
        body: opts.body,
      });
    } catch (e) {
      // Network-level failure: the backend is unreachable or restarting.
      this._setBackendDown(true);
      throw new ApiError('Cannot reach B3 Hive. Is the service still running?', 0);
    }
    // A dead backend behind the reverse proxy answers 502/503/504 with no JSON
    // body (decided below); any other status means the backend itself spoke.
    const proxyStatus = [502, 503, 504].includes(res.status);
    if (!proxyStatus) this._setBackendDown(false);

    if (res.status === 401) {
      this.session.authenticated = false;
      throw new ApiError('Your session expired — sign in again.', 401);
    }

    if (res.status === 423 && !opts._retried) {
      return await this.promptUnlockRetry(path, opts);
    }

    const data = await res.json().catch(() => ({}));
    if (proxyStatus) {
      // Our backend always includes {detail} (e.g. "node unavailable"); a bare
      // 5xx is the proxy saying there is nothing behind it.
      const backendSpoke = typeof data?.detail === 'string';
      this._setBackendDown(!backendSpoke);
      if (!backendSpoke) {
        throw new ApiError('Cannot reach B3 Hive. Is the service still running?', 0);
      }
    }
    if (!res.ok) throw new ApiError(friendlyError(res.status, data), res.status, data);
    return data;
  },

  /** Track backend reachability; announce recovery and refresh straight away. */
  _setBackendDown(down) {
    if (this.backend.down === down) return;
    this.backend.down = down;
    if (!down) {
      this.showToast('Reconnected to B3 Hive');
      this.pollNow?.();
    }
  },

  /** Probe whether an endpoint exists at all, without surfacing an error.
   *  Used for optional backend capabilities the UI can light up when added. */
  async apiSupports(path, method = 'POST') {
    try {
      const headers = {};
      const m = document.cookie.match(/(?:^|; )b3_csrf=([^;]+)/);
      if (m) headers['x-csrf-token'] = m[1];
      const res = await fetch(path, { method, headers, body: '{}' });
      // 404 / 405 mean "no such endpoint"; anything else means it exists
      // (including 400/403/422/423, which are real responses from a real route).
      return res.status !== 404 && res.status !== 405;
    } catch {
      return false;
    }
  },

  /* ------------------------------------------ point-of-action unlock (423) */

  /** Called by api() on 423. Shows the passphrase modal and returns a promise
   *  that settles when the user unlocks (retry) or cancels. */
  promptUnlockRetry(path, opts) {
    return new Promise((resolve, reject) => {
      this.unlockPrompt = {
        show: true, pw: '', busy: false, err: '', reveal: false,
        retry: { resolve, reject, path, opts },
      };
    });
  },

  async confirmUnlockPrompt() {
    const u = this.unlockPrompt;
    if (!u.pw) { u.err = 'Enter your wallet passphrase'; return; }
    u.busy = true; u.err = '';

    let unlocked = false;
    try {
// ECDH-seal the passphrase when envelope encryption is available
// (v0.5.0 transport security): the cleartext never crosses the wire.
const env = await seal(u.pw, 'b3hive-unlock');
      const r = await this.api('/api/wallet/unlock', {
        method: 'POST',
        body: JSON.stringify(env ? { env } : { passphrase: u.pw }),
        _retried: true, // an unlock call must never recurse into this modal
      });
      unlocked = true;
      this.session.wallet_unlocked = true;
      this.startUnlockCountdown(r.unlocked_for_s);
    } catch (e) {
      u.err = e.message; // wrong passphrase: keep the modal open
    }

    if (unlocked && u.retry) {
      const r = u.retry;
      u.retry = null;
      u.show = false; u.pw = '';
      r.opts._retried = true;
      try {
        r.resolve(await this.api(r.path, r.opts));
      } catch (e) {
        r.reject(e); // surface the action's own error to its original caller
      }
      this.refreshSession();
    }
    u.busy = false;
  },

  dismissUnlockPrompt() {
    const u = this.unlockPrompt;
    u.show = false; u.pw = ''; u.err = ''; u.reveal = false;
    if (u.retry) {
      u.retry.reject(new ApiError('Cancelled', 0, null, true));
      u.retry = null;
    }
  },
};

export class ApiError extends Error {
  constructor(message, status, data, cancelled = false) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
    this.cancelled = cancelled;
  }
}

/* Plain language, no RPC method names, no stack traces, no raw `detail`
   pass-through. Backend details are terse and often mention RPC internals
   ("node rejected loadwallet: ..."), so map by status first. */
const DETAIL_REWRITES = [
  [/wrong passphrase/i,                 'That passphrase is not correct.'],
  [/invalid credentials/i,              'That password is not correct.'],
  [/invalid code/i,                     'That authenticator code is not valid.'],
  [/too many attempts/i,                'Too many attempts. Wait a minute and try again.'],
  [/too many sign requests/i,           'Too many signing requests. Wait a minute and try again.'],
  [/wallet is locked/i,                 'Your wallet is locked.'],
  [/invalid B3 P2PKH address/i,         'That is not a valid B3 address.'],
  [/node unavailable/i,                 'Your node is not responding yet — it may still be starting or syncing.'],
  [/node unreachable/i,                 'Your node is not responding yet — it may still be starting or syncing.'],
  [/not persistent/i,                   'Wallet actions are disabled because the data directory is not persistent storage.'],
  [/cannot unload the last loaded wallet/i, 'This is your only loaded wallet, so it cannot be unloaded.'],
  [/plan changed since preview/i,       'Your coins changed since the preview. Run the preview again.'],
  [/confirm token mismatch/i,           'This confirmation is no longer valid. Run the preview again.'],
  [/rejected by mempool policy/i,       'The network would not accept this transaction. Try a different amount or fee.'],
  [/signing incomplete/i,               'The wallet could not sign this — it may have re-locked. Try again.'],
  [/insufficient funds/i,               'Not enough spendable B3 for this amount plus the fee.'],
  [/already pending/i,                  'Someone on the network is already creating one for this slot. Try again after the next block.'],
  [/cap is exhausted/i,                 'The lifetime FN Coin limit has been reached — no more can be created.'],
  [/asset id is unavailable/i,          'FN Coin is not configured on this chain.'],
  [/bootstrap already in progress/i,    'A chain download is already running.'],
  [/chain data already exists/i,        'Chain data is already on disk. Turn on "Replace existing chain data" to overwrite it.'],
  [/passphrase must be at least 8/i,    'The passphrase must be at least 8 characters.'],
  [/password must be at least 8/i,      'The password must be at least 8 characters.'],
  [/must be at least 8 characters/i,    'That must be at least 8 characters.'],
  [/new passphrase must differ/i,       'The new passphrase must be different from the current one.'],
  [/invalid wallet name|invalid wallet filename/i,
    'Use letters, numbers, dots, dashes and underscores only.'],
  [/set a login password before finishing/i,
    'Set your login password in the Security step before finishing setup.'],
  [/risk acknowledgement required/i,    'Tick the risk acknowledgement to continue.'],
  [/store a passphrase before enabling/i,
    'Save your wallet passphrase first, then enable unattended staking.'],
  [/CSRF/i,                             'Your session needs refreshing — reload the page and try again.'],
  [/2FA required/i,                     'Two-factor authentication is required for this action.'],
  [/setup required/i,                   'Finish first-run setup from the local machine first. (Docker: open the UI on the Docker host itself, or see B3_LOCAL_ADDRS in the README.)'],
  [/local access only/i,                'This can only be done from the machine running B3 Hive.'],
];

const STATUS_FALLBACK = {
  400: 'That request was not valid.',
  403: 'You are not allowed to do that.',
  404: 'That was not found.',
  409: 'That conflicts with the current state — refresh and try again.',
  422: 'Some of those values were not accepted.',
  429: 'Too many attempts. Wait a minute and try again.',
  500: 'Something went wrong inside B3 Hive.',
  502: 'Your node returned an error.',
  503: 'Your node is not available right now.',
};

function friendlyError(status, data) {
  const detail = typeof data?.detail === 'string' ? data.detail : '';
  for (const [re, msg] of DETAIL_REWRITES) {
    if (re.test(detail)) return msg;
  }
  if (status === 401) return 'Your session expired — sign in again.';
  if (STATUS_FALLBACK[status]) return STATUS_FALLBACK[status];
  // Last resort: a short, RPC-free remainder of the detail, or nothing.
  if (detail && detail.length < 120 && !/rpc|json|traceback|\bat \w+\.py/i.test(detail)) {
    return detail.charAt(0).toUpperCase() + detail.slice(1);
  }
  return 'That did not work. Please try again.';
}
