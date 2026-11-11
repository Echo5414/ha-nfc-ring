// Overview page of the NFC Ring integration: rings, tags and what each tag triggers.
// Plain web component, no build step. All data comes from the websocket command
// nfc_ring/overview; the page reloads it when a tag, automation or update changes.

const TEXT = {
  en: {
    title: "NFC Ring",
    rings: "Rings",
    tags: "Tags",
    name: "Name",
    firmware: "Firmware",
    status: "Status",
    lastScan: "Last scan",
    id: "ID",
    lastRing: "Last scanned by",
    automations: "Automations",
    active: "Active",
    disabled: "Deactivated",
    update: "Update to {v} available",
    upToDate: "up to date",
    never: "never",
    noRings: "No ring has reported yet. A ring appears here on its own as soon as it joins the Thread network.",
    noTags: "No tag has been scanned yet. Hold a ring to a tag and it appears here.",
    noAutomation: "no automation",
    createAutomation: "Create",
    off: "off",
    manageTags: "Manage tags",
    ringHint: "Select a ring to rename or deactivate it. A deactivated ring's scans are ignored.",
    error: "The overview could not be loaded.",
    justNow: "just now",
  },
  de: {
    title: "NFC Ring",
    rings: "Ringe",
    tags: "Tags",
    name: "Name",
    firmware: "Firmware",
    status: "Status",
    lastScan: "Letzter Scan",
    id: "Kennung",
    lastRing: "Zuletzt gescannt von",
    automations: "Automationen",
    active: "Aktiv",
    disabled: "Deaktiviert",
    update: "Update auf {v} verfügbar",
    upToDate: "aktuell",
    never: "noch nie",
    noRings: "Noch kein Ring hat sich gemeldet. Ein Ring erscheint hier von selbst, sobald er im Thread-Netzwerk ist.",
    noTags: "Noch kein Tag gescannt. Halte einen Ring an einen Tag, dann erscheint er hier.",
    noAutomation: "keine Automation",
    createAutomation: "Anlegen",
    off: "aus",
    manageTags: "Tags verwalten",
    ringHint: "Ring anklicken, um ihn umzubenennen oder zu deaktivieren. Scans eines deaktivierten Rings werden ignoriert.",
    error: "Die Übersicht konnte nicht geladen werden.",
    justNow: "gerade eben",
  },
};

const WATCHED = /^(tag|automation|update)\./;

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

class NfcRingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._error = false;
    this._loading = false;
    this._stamp = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
    // Reload only when something the page shows has changed.
    let stamp = "";
    for (const id in hass.states) {
      if (WATCHED.test(id)) stamp += hass.states[id].last_updated;
    }
    stamp += Object.keys(hass.devices || {}).length;
    if (first || stamp !== this._stamp) {
      this._stamp = stamp;
      this._load();
    }
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  connectedCallback() {
    this._render();
    // Relative times ("5 minutes ago") and renamed devices age without a state change.
    this._timer = setInterval(() => this._load(), 60000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  get _t() {
    const lang = (this._hass?.language || "en").split("-")[0];
    return TEXT[lang] || TEXT.en;
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      this._data = await this._hass.callWS({ type: "nfc_ring/overview" });
      this._error = false;
    } catch (err) {
      this._error = true;
    } finally {
      this._loading = false;
    }
    this._render();
  }

  _when(iso) {
    const t = this._t;
    if (!iso) return `<span class="muted">${t.never}</span>`;
    const date = new Date(iso);
    if (isNaN(date)) return esc(iso);
    const lang = this._hass?.language || "en";
    const seconds = (date - Date.now()) / 1000;
    let rel;
    if (Math.abs(seconds) < 45) {
      rel = t.justNow;
    } else {
      const fmt = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
      const steps = [["day", 86400], ["hour", 3600], ["minute", 60]];
      const [unit, size] = steps.find(([, s]) => Math.abs(seconds) >= s) || steps[2];
      rel = fmt.format(Math.round(seconds / size), unit);
    }
    const full = date.toLocaleString(lang, { dateStyle: "medium", timeStyle: "short" });
    return `<span title="${esc(full)}">${esc(rel)}</span>`;
  }

  _ringRows(rings) {
    const t = this._t;
    return rings
      .map((r) => {
        const status = r.disabled
          ? `<span class="pill off">${t.disabled}</span>`
          : `<span class="pill on">${t.active}</span>`;
        const fw = r.update_available
          ? `${esc(r.firmware || "–")} <span class="pill warn">${esc(t.update.replace("{v}", r.latest_firmware || "?"))}</span>`
          : `${esc(r.firmware || "–")} <span class="muted">· ${t.upToDate}</span>`;
        return `<tr class="${r.disabled ? "dim" : ""}">
          <td data-l="${t.name}"><div><a href="/config/devices/device/${esc(r.id)}">${esc(r.name)}</a></div></td>
          <td data-l="${t.status}"><div>${status}</div></td>
          <td data-l="${t.firmware}"><div>${fw}</div></td>
          <td data-l="${t.lastScan}"><div>${this._when(r.last_scan)}</div></td>
        </tr>`;
      })
      .join("");
  }

  _tagRows(tags) {
    const t = this._t;
    return tags
      .map((tag) => {
        const autos = tag.automations.length
          ? tag.automations
              .map((a) => {
                const href = a.id ? `/config/automation/edit/${encodeURIComponent(a.id)}` : "/config/automation/dashboard";
                const off = a.enabled ? "" : ` <span class="pill off">${t.off}</span>`;
                return `<div><a href="${esc(href)}">${esc(a.name)}</a>${off}</div>`;
              })
              .join("")
          : `<span class="muted">${t.noAutomation}</span>`;
        const ring = tag.device_id
          ? `<a href="/config/devices/device/${esc(tag.device_id)}">${esc(tag.device_name || tag.device_id)}</a>`
          : `<span class="muted">–</span>`;
        return `<tr>
          <td data-l="${t.name}"><div>${esc(tag.name)}</div></td>
          <td data-l="${t.id}" class="mono"><div>${esc(tag.id)}</div></td>
          <td data-l="${t.lastScan}"><div>${this._when(tag.last_scanned)}</div></td>
          <td data-l="${t.lastRing}"><div>${ring}</div></td>
          <td data-l="${t.automations}"><div>${autos}</div></td>
        </tr>`;
      })
      .join("");
  }

  _render() {
    const t = this._t;
    const data = this._data;
    let body;
    if (this._error && !data) {
      body = `<div class="card"><p class="empty">${t.error}</p></div>`;
    } else if (!data) {
      body = "";
    } else {
      const rings = data.rings.length
        ? `<div class="scroll"><table>
            <thead><tr><th>${t.name}</th><th>${t.status}</th><th>${t.firmware}</th><th>${t.lastScan}</th></tr></thead>
            <tbody>${this._ringRows(data.rings)}</tbody></table></div>
           <p class="hint">${t.ringHint}</p>`
        : `<p class="empty">${t.noRings}</p>`;
      const tags = data.tags.length
        ? `<div class="scroll"><table>
            <thead><tr><th>${t.name}</th><th>${t.id}</th><th>${t.lastScan}</th><th>${t.lastRing}</th><th>${t.automations}</th></tr></thead>
            <tbody>${this._tagRows(data.tags)}</tbody></table></div>`
        : `<p class="empty">${t.noTags}</p>`;
      body = `
        <section class="card">
          <h2>${t.rings} <span class="count">${data.rings.length}</span></h2>
          ${rings}
        </section>
        <section class="card">
          <h2>${t.tags} <span class="count">${data.tags.length}</span>
            <a class="action" href="/config/tags">${t.manageTags}</a></h2>
          ${tags}
        </section>`;
    }
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="bar"><ha-menu-button></ha-menu-button><h1>${t.title}</h1></div>
      <main>${body}</main>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    // Internal links: let Home Assistant navigate instead of reloading the app.
    this.shadowRoot.querySelectorAll('a[href^="/"]').forEach((a) =>
      a.addEventListener("click", (ev) => {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
        ev.preventDefault();
        history.pushState(null, "", a.getAttribute("href"));
        window.dispatchEvent(new CustomEvent("location-changed", { bubbles: true, composed: true }));
      })
    );
  }
}

const STYLE = `
  :host { display: block; min-height: 100vh; background: var(--primary-background-color); color: var(--primary-text-color);
          font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, sans-serif)); }
  .bar { display: flex; align-items: center; gap: 4px; height: var(--header-height, 56px); padding: 0 12px;
         background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff);
         position: sticky; top: 0; z-index: 2; box-sizing: border-box; }
  .bar h1 { font-size: 20px; font-weight: 400; margin: 0 0 0 8px; }
  main { max-width: 1100px; margin: 0 auto; padding: 16px; display: grid; gap: 16px; }
  .card { background: var(--ha-card-background, var(--card-background-color, #fff));
          border: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
          border-radius: var(--ha-card-border-radius, 12px); padding: 8px 0 12px; overflow: hidden; }
  h2 { display: flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 500; margin: 0; padding: 12px 20px; }
  .count { font-size: 13px; font-weight: 500; color: var(--secondary-text-color); background: var(--secondary-background-color);
           border-radius: 10px; padding: 1px 8px; }
  .action { margin-left: auto; font-size: 14px; font-weight: 500; }
  .scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; font-weight: 500; color: var(--secondary-text-color); font-size: 12px; text-transform: uppercase;
       letter-spacing: .04em; padding: 8px 20px; border-bottom: 1px solid var(--divider-color, #e0e0e0); white-space: nowrap; }
  td { padding: 12px 20px; border-bottom: 1px solid var(--divider-color, #e0e0e0); vertical-align: top; line-height: 1.5; }
  tbody tr:last-child td { border-bottom: none; }
  tr.dim td { color: var(--secondary-text-color); }
  a { color: var(--primary-color); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .mono { font-family: var(--ha-font-family-code, ui-monospace, Menlo, Consolas, monospace); font-size: 13px; word-break: break-all; }
  .muted { color: var(--secondary-text-color); }
  .pill { display: inline-block; font-size: 12px; font-weight: 500; line-height: 1.4; border-radius: 10px; padding: 1px 9px; white-space: nowrap; }
  .pill.on { color: var(--success-color, #2e7d32); background: color-mix(in srgb, var(--success-color, #2e7d32) 14%, transparent); }
  .pill.off { color: var(--secondary-text-color); background: var(--secondary-background-color); }
  .pill.warn { color: var(--warning-color, #b26a00); background: color-mix(in srgb, var(--warning-color, #b26a00) 16%, transparent); }
  .hint, .empty { color: var(--secondary-text-color); font-size: 13px; margin: 0; padding: 10px 20px 4px; }
  .empty { font-size: 14px; padding: 8px 20px 12px; }
  @media (max-width: 700px) {
    main { padding: 8px; }
    thead { display: none; }
    table, tbody, tr, td { display: block; }
    tr { padding: 8px 0; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
    tbody tr:last-child { border-bottom: none; }
    td { border: none; padding: 3px 20px; display: grid; grid-template-columns: 9.5em 1fr; gap: 8px; }
    td::before { content: attr(data-l); color: var(--secondary-text-color); font-size: 12px; }
  }
`;

customElements.define("nfc-ring-panel", NfcRingPanel);
