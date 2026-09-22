/* B3 Hive — notification settings: per-type enable/cooldown/channel prefs,
   and this device's push subscription. See app/notifier.py for the
   smart-coalescing model these controls drive. */

import { pushSupported, getExistingSubscription, enablePush, disablePush } from '../core/push.js';

export const notifMixin = {

  /** Registers the service worker on every load (no permission prompt) so
   *  an already-granted subscription from a previous visit keeps working
   *  even if the user never opens Settings this session. */
  async registerServiceWorker() {
    if (!pushSupported()) return;
    try { await navigator.serviceWorker.register('/sw.js'); } catch { /* not fatal */ }
  },

  async loadNotificationPrefs() {
    this.notif.busy = true;
    try {
      const r = await this.api('/api/notifications/prefs');
      this.notif.types = r.types;
      this.notif.loaded = true;
    } catch (e) { this.reportError(e); }
    this.notif.busy = false;
    this.refreshPushStatus();
    this.loadPushSubscriptions();
  },

  async refreshPushStatus() {
    if (!pushSupported()) { this.notif.pushSupported = false; return; }
    this.notif.pushSupported = true;
    this.notif.permission = Notification.permission; // 'default' | 'granted' | 'denied'
    const sub = await getExistingSubscription().catch(() => null);
    this.notif.subscribedHere = !!sub;
  },

  async loadPushSubscriptions() {
    try {
      const r = await this.api('/api/notifications/subscriptions');
      this.notif.subs = r.subscriptions;
    } catch (e) { /* non-fatal: the settings card still works without the device list */ }
  },

  async saveNotifPref(t) {
    t.busy = true;
    try {
      await this.api('/api/notifications/prefs/' + t.event_type, {
        method: 'PUT',
        body: JSON.stringify({
          enabled: t.enabled, cooldown_minutes: Number(t.cooldown_minutes) || 0,
          push: t.push, webhook: t.webhook,
        }),
      });
      this.showToast(t.label + ' updated');
    } catch (e) { this.reportError(e); }
    t.busy = false;
  },

  async enableDeviceNotifications() {
    this.notif.enrolling = true;
    try {
      await enablePush(this.api.bind(this));
      await this.refreshPushStatus();
      await this.loadPushSubscriptions();
      this.showToast('Push notifications enabled on this device');
    } catch (e) {
      this.showToast(e.message || 'Could not enable push notifications', 'danger');
    }
    this.notif.enrolling = false;
  },

  async disableDeviceNotifications() {
    this.notif.enrolling = true;
    try {
      await disablePush(this.api.bind(this));
      await this.refreshPushStatus();
      await this.loadPushSubscriptions();
      this.showToast('Push notifications turned off on this device');
    } catch (e) { this.reportError(e); }
    this.notif.enrolling = false;
  },

  async revokePushSubscription(id) {
    try {
      await this.api('/api/notifications/subscriptions/' + id, { method: 'DELETE' });
      await this.loadPushSubscriptions();
      await this.refreshPushStatus();
      this.showToast('Device removed');
    } catch (e) { this.reportError(e); }
  },

  async sendTestNotification(eventType) {
    try {
      await this.api('/api/notifications/test', {
        method: 'POST', body: JSON.stringify({ event_type: eventType }),
      });
      this.showToast('Test notification sent');
    } catch (e) { this.reportError(e); }
  },

  cooldownLabel(minutes) {
    const m = Number(minutes) || 0;
    if (m <= 0) return 'Instant';
    if (m < 60) return m + ' min';
    if (m % 1440 === 0) return (m / 1440) + 'd';
    return (m / 60) + 'h';
  },
};
