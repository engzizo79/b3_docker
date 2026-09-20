/* B3 Hive — ECDH envelope encryption for secrets in transit (v0.5.0).

 Mirrors backend/app/crypto_envelope.py exactly:
 - Curve P-256, raw uncompressed public keys (0x04||x||y), base64.
 - ECDH shared secret -> HKDF-SHA256 (32 bytes, salt + info) -> AES-GCM 256.
 - Envelope fields: client_pub, iv, ct, salt, info (all base64).

 Used by login (password) and wallet unlock (passphrase) so the cleartext
 secret never crosses the wire — defense in depth for plain-HTTP remote
 access. The server key is fetched once per page load from /api/auth/pubkey;
 when that endpoint is 404 (ENVELOPE_ENCRYPTION=false) or crypto.subtle is
 unavailable (plain HTTP non-localhost contexts in some browsers), seal()
 resolves to null and callers fall back to the legacy plain field. */

let serverPubRaw = null; // ArrayBuffer of the raw server public key
let serverPubB64 = null;
let fetchPromise = null;

function b64(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

function unb64(s) {
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function ensureKey() {
  if (serverPubRaw !== null) return serverPubRaw !== undefined ? serverPubRaw : null;
  if (!fetchPromise) {
    fetchPromise = fetch('/api/auth/pubkey')
      .then(r => (r.ok ? r.json() : null))
      .then(j => {
        if (!j || !j.pubkey) { serverPubRaw = undefined; return null; }
        serverPubB64 = j.pubkey;
        serverPubRaw = unb64(j.pubkey).buffer;
        return serverPubRaw;
      })
      .catch(() => { serverPubRaw = undefined; return null; });
  }
  return fetchPromise;
}

/** Seal a secret string into an ECDH envelope, or null when unavailable. */
export async function seal(secret, infoStr = 'b3hive-secret') {
  try {
    if (!window.crypto || !crypto.subtle) return null;
    const spub = await ensureKey();
    if (!spub) return null;

    const serverKey = await crypto.subtle.importKey(
      'raw', spub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
    const clientPair = await crypto.subtle.generateKey(
      { name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
    const clientPubRaw = await crypto.subtle.exportKey('raw', clientPair.publicKey);

    const shared = await crypto.subtle.deriveBits(
      { name: 'ECDH', namedCurve: 'P-256', public: serverKey },
      clientPair.privateKey, 256);

    const salt = crypto.getRandomValues(new Uint8Array(16));
    const info = new TextEncoder().encode(infoStr);
    const key = await crypto.subtle.importKey(
      'raw', shared, { name: 'HKDF' }, false, ['deriveKey']);
    const aesKey = await crypto.subtle.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt, info }, key,
      { name: 'AES-GCM', length: 256 }, false, ['encrypt']);

    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv }, aesKey, new TextEncoder().encode(secret));

    return {
      client_pub: b64(clientPubRaw),
      iv: b64(iv),
      ct: b64(ct),
      salt: b64(salt),
      info: b64(info),
    };
  } catch {
    return null; // caller falls back to the plain field
  }
}
