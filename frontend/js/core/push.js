/* B3 Hive — Web Push subscribe/unsubscribe plumbing.

   Pure browser-API helpers, kept separate from the Alpine mixin (features/
   notifications.js) so the subscribe flow can be unit-reasoned about
   without Alpine in the picture. `api` is always the component's own
   api() (core/api.js) — passed in rather than imported, so this stays a
   plain function module. */

export function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

/** The existing PushSubscription for this browser, or null. Never prompts. */
export async function getExistingSubscription() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration('/');
  if (!reg) return null;
  return reg.pushManager.getSubscription();
}

/** Registers the service worker, asks for permission, subscribes with the
 *  server's VAPID key, and tells the backend about the new subscription.
 *  Throws with a plain-language message on any refusal/failure. */
export async function enablePush(api) {
  if (!pushSupported()) {
    throw new Error('This browser does not support push notifications.');
  }
  const reg = await navigator.serviceWorker.register('/sw.js');
  await navigator.serviceWorker.ready;

  const perm = await Notification.requestPermission();
  if (perm !== 'granted') {
    throw new Error('Notification permission was not granted.');
  }

  const { key } = await api('/api/notifications/vapid-public-key');
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
  }
  const json = sub.toJSON();
  await api('/api/notifications/subscribe', {
    method: 'POST',
    body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys, user_agent: navigator.userAgent }),
  });
  return sub;
}

/** Unsubscribes this browser and tells the backend to forget it. */
export async function disablePush(api) {
  const sub = await getExistingSubscription();
  if (!sub) return;
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  await api('/api/notifications/unsubscribe', {
    method: 'POST',
    body: JSON.stringify({ endpoint }),
  });
}

/** VAPID applicationServerKey must be a Uint8Array; the server hands out
 *  the standard base64url encoding. */
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
