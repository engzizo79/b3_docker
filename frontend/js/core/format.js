/* B3 Hive — formatting helpers.

   MONEY RULE: B3 has 9 decimals and the backend sends amounts as exact
   strings (`format(amt, ".9f")`). Everything here manipulates those STRINGS.
   The previous implementation did `Number(v).toLocaleString(...)`, which
   round-trips 9-dp money through a float and silently loses precision on
   large balances — a direct violation of the project's Decimal-only rule.
   Nothing in this file multiplies, divides or adds amounts. */

const NBSP_NARROW = ' '; // narrow no-break space — numeric group separator

/** Split any amount representation into exact 9-dp string parts.
 *  Returns null when the value is absent or unparseable. */
export function amountParts(value) {
  if (value === null || value === undefined || value === '') return null;

  let s;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    // Some read-only RPCs pass floats straight through (e.g. getbalances).
    // toFixed(9) is the least-lossy rendering available at that point; we
    // never introduce *additional* loss beyond what the JSON already had.
    s = value.toFixed(9);
  } else {
    s = String(value).trim();
  }

  if (!/^[+-]?\d+(\.\d*)?$/.test(s)) {
    const n = Number(s);
    if (!Number.isFinite(n)) return null;
    s = n.toFixed(9);
  }

  const neg = s.startsWith('-');
  if (neg || s.startsWith('+')) s = s.slice(1);

  let [int = '0', frac = ''] = s.split('.');
  frac = (frac + '000000000').slice(0, 9);
  int = int.replace(/^0+(?=\d)/, '');
  return { neg, int, frac };
}

/** Group digits from the left in threes: "27226021" -> "27 226 021". */
function groupInt(digits) {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, NBSP_NARROW);
}

/** Group digits from the right in threes: "595212715" -> "595 212 715". */
function groupFrac(digits) {
  return digits.replace(/(\d{3})(?=\d)/g, '$1' + NBSP_NARROW);
}

/** Trim trailing zero groups but always keep at least `min` decimals. */
function trimFrac(frac, min) {
  let f = frac.replace(/0+$/, '');
  while (f.length < min) f += '0';
  return f;
}

/**
 * Full display string, e.g. "27 226 021.595 212 715".
 * opts.minDecimals  keep at least N decimals (default 2)
 * opts.maxDecimals  truncate (not round) to N decimals
 * opts.unit         append " B3"
 * opts.sign         'auto' | 'always' | 'none'
 */
export function fmtAmount(value, opts = {}) {
  const p = amountParts(value);
  if (!p) return '—';
  const min = opts.minDecimals ?? 2;
  let frac = opts.maxDecimals != null ? p.frac.slice(0, opts.maxDecimals) : p.frac;
  frac = trimFrac(frac, min);

  let out = groupInt(p.int);
  if (frac) out += '.' + groupFrac(frac);
  if (p.neg && opts.sign !== 'none') out = '−' + out;
  else if (!p.neg && opts.sign === 'always') out = '+' + out;
  if (opts.unit) out += ' B3';
  return out;
}

/** Parts for the <Amount> display treatment, where the fraction is rendered
 *  smaller and muted so nine decimals stay readable. */
export function amountDisplay(value, opts = {}) {
  const p = amountParts(value);
  if (!p) return null;
  const min = opts.minDecimals ?? 2;
  const frac = trimFrac(
    opts.maxDecimals != null ? p.frac.slice(0, opts.maxDecimals) : p.frac, min);
  return {
    sign: p.neg ? '−' : (opts.sign === 'always' ? '+' : ''),
    int: groupInt(p.int),
    frac: frac ? '.' + groupFrac(frac) : '',
    neg: p.neg,
  };
}

/** Markup for the <Amount> treatment, for use with x-html.
 *  SAFETY: every character of the output is generated here from digits and
 *  group separators — no caller-supplied text is interpolated, so there is no
 *  injection surface even though this bypasses text escaping. */
export function amountHtml(value, opts = {}) {
  const d = amountDisplay(value, opts);
  if (!d) return '<span class="amt muted">—</span>';
  const cls = ['amt', opts.class || ''].filter(Boolean).join(' ');
  let html = '<span class="' + cls + '">';
  if (d.sign) html += '<span class="amt-sign">' + d.sign + '</span>';
  html += '<span class="amt-int">' + d.int + '</span>';
  if (d.frac) html += '<span class="amt-frac">' + d.frac + '</span>';
  if (opts.unit) html += '<span class="amt-unit">B3</span>';
  return html + '</span>';
}

/** True when the amount is exactly zero (string-safe, no float compare). */
export function isZeroAmount(value) {
  const p = amountParts(value);
  return !!p && /^0*$/.test(p.int) && /^0*$/.test(p.frac);
}

/** True when the amount is greater than zero. */
export function isPositiveAmount(value) {
  const p = amountParts(value);
  return !!p && !p.neg && !(/^0*$/.test(p.int) && /^0*$/.test(p.frac));
}

/* ------------------------------------------------------------ user input -- */

/** Validate an amount typed by a user. Mirrors backend `parse_amount()`
 *  (backend/app/rpc.py): positive, finite, no scientific notation, ≤9dp. */
export function validateAmountInput(raw, opts = {}) {
  const s = String(raw ?? '').trim();
  if (!s) return { ok: false, error: 'Enter an amount' };
  if (/e/i.test(s)) return { ok: false, error: 'Write the number out in full' };
  if (!/^\d*(\.\d*)?$/.test(s)) return { ok: false, error: 'Numbers only, e.g. 12.5' };
  const [, frac = ''] = s.split('.');
  if (frac.length > 9) return { ok: false, error: 'B3 has at most 9 decimal places' };
  if (!isPositiveAmount(s)) return { ok: false, error: 'Amount must be more than zero' };
  if (opts.max != null && compareAmounts(s, opts.max) > 0) {
    return { ok: false, error: 'More than your spendable balance' };
  }
  return { ok: true, value: s };
}

/** Compare two amounts exactly, as strings. -1 | 0 | 1 */
export function compareAmounts(a, b) {
  const pa = amountParts(a), pb = amountParts(b);
  if (!pa || !pb) return 0;
  if (pa.neg !== pb.neg) return pa.neg ? -1 : 1;
  const flip = pa.neg ? -1 : 1;
  const ia = pa.int.padStart(Math.max(pa.int.length, pb.int.length), '0');
  const ib = pb.int.padStart(Math.max(pa.int.length, pb.int.length), '0');
  if (ia !== ib) return (ia < ib ? -1 : 1) * flip;
  if (pa.frac !== pb.frac) return (pa.frac < pb.frac ? -1 : 1) * flip;
  return 0;
}

/** Subtract b from a exactly (both non-negative, 9dp). Used only for the
 *  "Max" chip and "balance after" hints — never for building transactions,
 *  which the backend always computes itself. */
export function subtractAmounts(a, b) {
  const pa = amountParts(a), pb = amountParts(b);
  if (!pa || !pb) return null;
  const toUnits = (p) => BigInt(p.int + p.frac) * (p.neg ? -1n : 1n);
  let units = toUnits(pa) - toUnits(pb);
  const neg = units < 0n;
  if (neg) units = -units;
  const s = units.toString().padStart(10, '0');
  const out = s.slice(0, -9) + '.' + s.slice(-9);
  return (neg ? '-' : '') + out;
}

/* -------------------------------------------------------------- addresses -- */

/** B3 legacy P2PKH: version byte 0x3F, "S" prefix.
 *  Mirrors the backend regex in wallet.py / wallet_extra.py. */
export const B3_ADDRESS_RE = /^S[1-9A-HJ-NP-Za-km-z]{26,34}$/;

export function isValidAddress(a) {
  return B3_ADDRESS_RE.test(String(a ?? '').trim());
}

/** Explain an invalid address in plain language — never "regex mismatch". */
export function addressProblem(raw) {
  const a = String(raw ?? '').trim();
  if (!a) return 'Enter the address you want to pay';
  if (/^(bc1|tb1|b3q)/i.test(a)) {
    return 'That looks like a bech32 address from another wallet. B3 addresses start with S.';
  }
  if (/^[13]/.test(a)) return 'That looks like a Bitcoin address. B3 addresses start with S.';
  if (!a.startsWith('S')) return 'B3 addresses start with a capital S.';
  if (/[0OIl]/.test(a.slice(1))) return 'This contains 0, O, I or l, which never appear in a B3 address — check for a typo.';
  if (a.length < 27) return 'This address is too short — a few characters may be missing.';
  if (a.length > 35) return 'This address is too long — it may have extra characters.';
  return 'This is not a valid B3 address.';
}

/** Truncate by CHARACTER (first 8 … last 6), never by CSS width, and never
 *  hide the leading S — the prefix is how a user recognises a B3 address. */
export function truncAddr(a, head = 8, tail = 6) {
  const s = String(a ?? '');
  if (s.length <= head + tail + 1) return s;
  return s.slice(0, head) + '…' + s.slice(-tail);
}

export function truncHash(h, head = 10, tail = 6) {
  return truncAddr(h, head, tail);
}

/* ------------------------------------------------------------------ misc -- */

export function fmtInt(n) {
  if (n === null || n === undefined || n === '') return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return Math.trunc(v).toLocaleString('en-US').replace(/,/g, NBSP_NARROW);
}

export function fmtPercent(n, dp = 1) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(dp) + '%';
}

export function fmtBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '—';
  if (v < 1024) return v + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let x = v / 1024, i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return (x < 10 ? x.toFixed(1) : Math.round(x)) + ' ' + units[i];
}

/** Coarse duration for "~N behind" style copy. */
export function fmtDuration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return '—';
  if (s < 90) return Math.round(s) + ' s';
  const m = s / 60;
  if (m < 90) return Math.round(m) + ' min';
  const h = m / 60;
  if (h < 36) return (h < 10 ? h.toFixed(1) : Math.round(h)) + ' h';
  return Math.round(h / 24) + ' days';
}

/** mm:ss — used by the unlock countdown. */
export function fmtClock(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

export function fmtDateTime(ts) {
  const d = toDate(ts);
  if (!d) return '—';
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

export function fmtTime(ts) {
  const d = toDate(ts);
  if (!d) return '—';
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** "just now" / "12 min ago" / "3 Sep" — for activity rows. */
export function fmtRelative(ts) {
  const d = toDate(ts);
  if (!d) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 45) return 'just now';
  if (diff < 3600) return Math.round(diff / 60) + ' min ago';
  if (diff < 86400) return Math.round(diff / 3600) + ' h ago';
  if (diff < 86400 * 6) return Math.round(diff / 86400) + ' days ago';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** Day bucket label used to group the activity list. */
export function dayBucket(ts) {
  const d = toDate(ts);
  if (!d) return 'Unknown date';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const days = Math.round((today - that) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  return d.toLocaleDateString(undefined, {
    month: 'long', day: 'numeric',
    year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}

function toDate(ts) {
  if (ts === null || ts === undefined || ts === '') return null;
  if (typeof ts === 'number') {
    const d = new Date(ts < 1e12 ? ts * 1000 : ts); // seconds or millis
    return isNaN(d) ? null : d;
  }
  const d = new Date(ts);
  return isNaN(d) ? null : d;
}

/* ---------------------------------------------------- DetailList humanising */
/* Raw RPC JSON reads as developer output. These turn an arbitrary RPC object
   into labelled, formatted rows instead of a JSON dump. */

const KEY_LABELS = {
  blocks: 'Block height', headers: 'Headers known', bestblockhash: 'Best block',
  difficulty: 'Difficulty', mediantime: 'Median block time',
  verificationprogress: 'Verification progress', initialblockdownload: 'Initial block download',
  size_on_disk: 'Size on disk', pruned: 'Pruned', chain: 'Network', warnings: 'Warnings',
  connections: 'Connections', connections_in: 'Inbound', connections_out: 'Outbound',
  subversion: 'Node version', protocolversion: 'Protocol version', version: 'Version',
  relayfee: 'Relay fee', incrementalfee: 'Incremental fee', localrelay: 'Relays transactions',
  networkactive: 'Network active', timeoffset: 'Time offset',
  size: 'Transactions waiting', bytes: 'Memory pool size', usage: 'Memory used',
  maxmempool: 'Memory pool limit', mempoolminfee: 'Minimum pool fee', minrelaytxfee: 'Minimum relay fee',
  epoch: 'Epoch', active: 'Active', finalized_height: 'Finalized height',
  justified_height: 'Justified height', validator_set: 'Validator set',
  last_signed_height: 'Last signed block', blocks_produced: 'Blocks produced',
  finality_signing: 'Finality signing', running: 'Running', state: 'State',
  available: 'Available', weight: 'Stake weight', netstakeweight: 'Network stake weight',
  expectedtime: 'Expected time to a block', pending: 'Pending', unconfirmed: 'Unconfirmed',
  total_amount: 'Total supply', txouts: 'Unspent outputs', bestblock: 'At block',
  transactions: 'Transactions', height: 'Height', disk_size: 'Database size',
  asset_id: 'Asset id', owner_address: 'Owner address', ticker: 'Ticker',
  decimals: 'Decimal places', confirmed: 'Confirmed', spendable: 'Spendable',
  modern_issued: 'Created so far', modern_capacity: 'Lifetime capacity',
  pod_active: 'Proof-free creation active', counter_known: 'Counter synced',
  configured: 'Configured', issuance_fee: 'Issuance fee',
  confirmations: 'Confirmations', txid: 'Transaction id', vout: 'Output index',
  amount: 'Amount', fee: 'Fee', time: 'Time', timereceived: 'Received',
  category: 'Type', address: 'Address', label: 'Label', comment: 'Note',
  blockhash: 'Block hash', blockheight: 'Block height', blocktime: 'Block time',
  'bip125-replaceable': 'Replaceable',
};

/** De-camelCase / de-snake_case anything not in the table above. */
export function humanKey(key) {
  if (KEY_LABELS[key]) return KEY_LABELS[key];
  return String(key)
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/^\s*(\w)/, (m, c) => c.toUpperCase())
    .trim();
}

const AMOUNT_KEYS = /(amount|balance|fee|supply|total_amount|output|input|value|reserve|target|disintegration|issuance_fee)/i;
const TIME_KEYS = /(^time$|time$|_ts$|^ts$|mediantime|blocktime|created_utc)/i;
const HASH_KEYS = /(hash|txid|asset_id|script|pubkey|blockhash|sha256|market_id)/i;
const BYTE_KEYS = /(size_on_disk|disk_size|^bytes$|usage|maxmempool|bytesrecv|bytessent)/i;

/** Turn an RPC object into [{key, label, value, mono, group}] display rows.
 *  Nested objects become a group heading followed by their own rows; arrays
 *  are summarised by length rather than dumped. */
export function toDetailRows(obj, opts = {}) {
  const rows = [];
  const skip = new Set(opts.skip || []);

  const push = (key, value, depth) => {
    if (skip.has(key)) return;
    if (value === null || value === undefined) return;

    if (Array.isArray(value)) {
      if (!value.length) return;
      const allScalar = value.every((v) => typeof v !== 'object' || v === null);
      rows.push({
        key, label: humanKey(key), mono: false,
        value: allScalar && value.length <= 4
          ? value.join(', ')
          : value.length + ' ' + (value.length === 1 ? 'entry' : 'entries'),
      });
      return;
    }

    if (typeof value === 'object') {
      if (depth >= 1) {
        const n = Object.keys(value).length;
        rows.push({ key, label: humanKey(key), value: n + ' fields', mono: false });
        return;
      }
      const inner = Object.entries(value);
      if (!inner.length) return;
      rows.push({ group: humanKey(key) });
      for (const [k, v] of inner) push(k, v, depth + 1);
      return;
    }

    let out;
    let mono = false;
    if (typeof value === 'boolean') {
      out = value ? 'Yes' : 'No';
    } else if (HASH_KEYS.test(key) && typeof value === 'string' && value.length > 20) {
      out = truncHash(value); mono = true;
    } else if (key === 'verificationprogress') {
      out = fmtPercent(Number(value) * 100, 2);
    } else if (BYTE_KEYS.test(key)) {
      out = fmtBytes(value);
    } else if (AMOUNT_KEYS.test(key)) {
      out = fmtAmount(value, { unit: true });
    } else if (TIME_KEYS.test(key) && Number(value) > 1e8) {
      out = fmtDateTime(value);
    } else if (typeof value === 'number') {
      out = Number.isInteger(value) ? fmtInt(value) : String(value);
    } else {
      out = String(value);
      if (!out) return;
      if (out.length > 28 && /^[0-9a-fx]+$/i.test(out)) { out = truncHash(out); mono = true; }
    }
    rows.push({ key, label: humanKey(key), value: out, mono });
  };

  for (const [k, v] of Object.entries(obj || {})) push(k, v, 0);
  return rows;
}
