/* B3 Hive - contacts: saved recipient addresses for the Send picker.

   Convenience only. A contact never bypasses the Send review step, which
   always shows the full address. Stored in the backend database, so they
   follow the user across browsers. */

import { isValidAddress } from '../core/format.js';

export const contactsMixin = {

  async loadContacts() {
    try {
      const d = await this.api('/api/contacts');
      this.contacts.list = d.contacts || [];
      this.contacts.error = false;
    } catch {
      this.contacts.error = true;
    }
    this.contacts.loaded = true;
  },

  /** The saved contact for an address, if any. */
  contactFor(address) {
    return (this.contacts.list || []).find((c) => c.address === address) || null;
  },

  /** True when the address typed on Send is valid, not yet saved and not
   *  one of the wallet's own addresses (those are in "My addresses"). */
  canSaveRecipient() {
    const v = (this.send.to || '').trim();
    return isValidAddress(v) && !this.contactFor(v)
      && !(this.book.addresses || []).some((a) => a.address === v);
  },

  contactResults() {
    const q = (this.send.pickerQuery || '').trim().toLowerCase();
    const list = this.contacts.list || [];
    if (!q) return list;
    return list.filter((c) =>
      c.label.toLowerCase().includes(q) || c.address.toLowerCase().includes(q));
  },

  async addContact(label, address) {
    const name = (label || '').trim();
    const addr = (address || '').trim();
    if (!name) { this.showToast('Give the contact a name', 'warning'); return false; }
    if (!isValidAddress(addr)) { this.showToast('That is not a valid B3 address', 'warning'); return false; }
    try {
      await this.api('/api/contacts', {
        method: 'POST', body: JSON.stringify({ label: name, address: addr }),
      });
      this.showToast('Saved “' + name + '” to contacts');
      await this.loadContacts();
      return true;
    } catch (e) {
      // The backend's message is specific ("already in your contacts").
      const msg = e && e.data && typeof e.data.detail === 'string' ? e.data.detail : '';
      if (msg) this.showToast(msg, 'warning'); else this.reportError(e);
      return false;
    }
  },

  /** Add form on the Addresses page. */
  async submitContactForm() {
    const f = this.contacts.form;
    if (this.contacts.busy) return;
    this.contacts.busy = true;
    if (await this.addContact(f.label, f.address)) this.contacts.form = { label: '', address: '' };
    this.contacts.busy = false;
  },

  /** Inline "save this recipient" on Send (step 1) and after a payment. */
  async saveRecipient() {
    if (this.contacts.busy) return;
    this.contacts.busy = true;
    if (await this.addContact(this.contacts.saveName, this.send.to)) this.contacts.saveName = '';
    this.contacts.busy = false;
  },

  startContactRename(c) { this.contacts.editing = c.id; this.contacts.draft = c.label; },
  cancelContactRename() { this.contacts.editing = null; this.contacts.draft = ''; },

  async saveContactRename(c) {
    const label = (this.contacts.draft || '').trim();
    if (!label) { this.showToast('Give the contact a name', 'warning'); return; }
    try {
      await this.api('/api/contacts/' + c.id, {
        method: 'PUT', body: JSON.stringify({ label }),
      });
      this.contacts.editing = null;
      this.showToast('Renamed to “' + label + '”');
      this.loadContacts();
    } catch (e) { this.reportError(e); }
  },

  deleteContact(c) {
    this.confirm({
      title: 'Remove “' + c.label + '”?',
      body: 'This only removes the name from your contacts. Nothing is sent, and the address itself is unaffected.',
      confirmLabel: 'Remove',
      danger: true,
      run: async () => {
        try {
          await this.api('/api/contacts/' + c.id, { method: 'DELETE' });
          this.showToast('Contact removed');
          this.loadContacts();
        } catch (e) { this.reportError(e); }
      },
    });
  },

  pickContact(c) {
    this.send.to = c.address;
    this.send.label = c.label;
    this.send.pickerOpen = false;
    this.send.err = '';
  },
};
