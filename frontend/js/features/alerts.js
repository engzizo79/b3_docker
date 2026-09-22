/* B3 Hive — alerts.

   The bell is a Dropdown with proper aria-expanded / aria-haspopup and
   click-away handling. Alert levels from app/monitor.py are 'stall',
   'recovery' and informational strings, so the mapping stays tolerant. */

import { fmtRelative } from '../core/format.js';

export const alertsMixin = {

  fmtRelative,

  async loadAlerts() {
    if (!this.session.authenticated) return;
    try {
      const r = await this.api('/api/alerts?unacked=true');
      this.alerts = r.alerts || [];
      this.alertCount = r.count || 0;
    } catch { /* silent: alerts are never the reason to interrupt someone */ }
  },

  openAlerts() {
    this.showAlerts = true;
    this.loadAlerts();
  },

  toggleAlerts() {
    this.showAlerts = !this.showAlerts;
    if (this.showAlerts) this.loadAlerts();
  },

  async ackAlert(id) {
    try {
      await this.api('/api/alerts/' + id + '/ack', { method: 'POST' });
      this.alerts = this.alerts.filter((a) => a.id !== id);
      this.alertCount = Math.max(0, this.alertCount - 1);
    } catch (e) { this.reportError(e); }
  },

  async ackAllAlerts() {
    try {
      await this.api('/api/alerts/ack-all', { method: 'POST' });
      this.alerts = [];
      this.alertCount = 0;
      this.showAlerts = false;
      this.showToast('All alerts cleared');
    } catch (e) { this.reportError(e); }
  },

  alertBadgeClass(level) {
    const l = String(level || '').toLowerCase();
    if (l === 'stall' || l === 'warning' || l === 'lag') return 'badge-warning';
    if (l === 'recovery' || l === 'ok' || l === 'received' || l === 'stake') return 'badge-success';
    if (l === 'error' || l === 'critical') return 'badge-danger';
    return 'badge-info';
  },

  alertLevelWord(level) {
    const l = String(level || '').toLowerCase();
    const map = {
      stall: 'Stalled', recovery: 'Recovered', warning: 'Warning',
      error: 'Error', critical: 'Critical', info: 'Info', lag: 'Sync lag',
      received: 'Received', sent: 'Sent', stake: 'Staking reward',
    };
    return map[l] || (level ? String(level) : 'Notice');
  },
};
