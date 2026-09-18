/* B3 Hive — QR rendering.

   Thin wrapper over the vendored qrcode.min.js (loaded as a classic script,
   so it lives on window). Kept in one place because both the receive address
   and the TOTP provisioning URI need it, and both previously duplicated the
   same canvas-clearing dance. */

export function renderQR(containerId, text, size = 184) {
  const el = document.getElementById(containerId);
  if (!el || !text) return false;
  if (typeof window.QRCode === 'undefined') {
    el.textContent = 'QR unavailable';
    return false;
  }
  el.innerHTML = '';
  try {
    // eslint-disable-next-line no-new
    new window.QRCode(el, {
      text: String(text),
      width: size,
      height: size,
      // Fixed black-on-white: the container is always white so the code stays
      // scannable in both themes.
      colorDark: '#000000',
      colorLight: '#ffffff',
    });
    return true;
  } catch {
    el.textContent = 'Could not draw QR code';
    return false;
  }
}
