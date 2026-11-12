// Overview page of the NFC Ring integration: rings, tags and what each tag triggers.
// Plain web component, no build step. All data comes from the websocket command
// nfc_ring/overview; the page reloads it when a tag, automation or update changes.
// A new ring is paired by entering the code from its packaging (nfc_ring/pair);
// its radio is chosen in the table (nfc_ring/set_radio). A ring is deactivated
// or deleted through Home Assistant's device registry, like on its device page.

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
    noRings: "No ring yet. Switch a new ring on and enter the code from its packaging below.",
    pairing: "Pairing",
    paired: "Paired",
    unpaired: "Not paired",
    unpairedHint: "Set up before pairing existed. Update its firmware, then enter its code below.",
    unpairedReady: "Enter its code below to secure it.",
    needsCode: "Waiting for code",
    tooOld: "Firmware too old",
    tooOldHint: "This ring cannot be paired yet. It needs firmware 0.4.0 or newer.",
    newRing: "New ring {id}",
    addRing: "Add a ring",
    addHint: "Enter the code from the ring's packaging. Only this code lets a ring trigger anything here.",
    pair: "Pair ring",
    scan: "Scan code",
    scanTitle: "Scan the ring's code",
    scanHint: "Point the camera at the code on the ring's packaging.",
    scanManual: "Type it instead",
    scanWrong: "This is not a ring's pairing code.",
    pairedNow: "The ring is paired.",
    pairingNow: "Code accepted. The ring is paired as soon as it reports or reads a tag, usually within a minute.",
    pairingPill: "Pairing…",
    pairingHint: "Code accepted. Waiting for the ring to report or read a tag.",
    waiting: "Code saved. The ring is paired as soon as it reports or reads a tag, usually within a minute. Switch it on if it is off.",
    invalidCode: "This is not a pairing code. It has 20 characters, for example ABCDE-12345-FGHJK-67890.",
    pairFailed: "Pairing did not work: {e}",
    noTags: "No tag has been scanned yet. Hold a ring to a tag and it appears here.",
    noAutomation: "no automation",
    createAutomation: "Create",
    off: "off",
    manageTags: "Manage tags",
    ringHint: "A deactivated ring's scans are ignored. A deleted ring needs its code again. Select a ring's name to rename it.",
    actions: "Actions",
    deactivate: "Deactivate",
    activate: "Activate",
    remove: "Delete",
    removeSure: "Really delete?",
    removed: "The ring is deleted. To use it again, enter its code.",
    removeReset: "Delete and reset ring",
    resetHint: "The ring also forgets the Thread network and Home Assistant and restarts with its factory settings. It has to be set up again afterwards. Its code stays the same.",
    removedReset: "The ring is deleted and back to its factory settings. It has to be set up again before it can be paired.",
    resetFailed: "The ring was not reset and not deleted: {e}",
    actionFailed: "That did not work: {e}",
    deletedRing: "deleted ring",
    error: "The overview could not be loaded.",
    radio: "Radio",
    radioThread: "Thread",
    radioBluetooth: "Bluetooth",
    radioZigbee: "Zigbee",
    radioZigbeeSoon: "Zigbee (not available yet)",
    radioWaitsContact: "The ring switches at its next contact.",
    radioWaitsUpdate: "Update the firmware; the ring switches by itself afterwards.",
    radioViaBluetooth: "Not in the Thread network yet; reached over Bluetooth.",
    radioFailed: "The radio was not changed: {e}",
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
    noRings: "Noch kein Ring. Neuen Ring einschalten und unten den Code von der Verpackung eingeben.",
    pairing: "Kopplung",
    paired: "Gekoppelt",
    unpaired: "Nicht gekoppelt",
    unpairedHint: "Eingerichtet, bevor es die Kopplung gab. Erst Firmware aktualisieren, dann unten den Code eingeben.",
    unpairedReady: "Zum Absichern unten den Code eingeben.",
    needsCode: "Wartet auf Code",
    tooOld: "Firmware zu alt",
    tooOldHint: "Dieser Ring lässt sich noch nicht koppeln. Er braucht Firmware 0.4.0 oder neuer.",
    newRing: "Neuer Ring {id}",
    addRing: "Ring hinzufügen",
    addHint: "Code von der Verpackung des Rings eingeben. Nur mit diesem Code kann ein Ring hier etwas auslösen.",
    pair: "Ring koppeln",
    scan: "Code scannen",
    scanTitle: "Code des Rings scannen",
    scanHint: "Kamera auf den Code auf der Verpackung des Rings richten.",
    scanManual: "Lieber eintippen",
    scanWrong: "Das ist kein Kopplungscode eines Rings.",
    pairedNow: "Der Ring ist gekoppelt.",
    pairingNow: "Code angenommen. Der Ring wird gekoppelt, sobald er sich meldet oder einen Tag liest, meist innerhalb einer Minute.",
    pairingPill: "Wird gekoppelt …",
    pairingHint: "Code angenommen. Wartet darauf, dass der Ring sich meldet oder einen Tag liest.",
    waiting: "Code gespeichert. Der Ring wird gekoppelt, sobald er sich meldet oder einen Tag liest, meist innerhalb einer Minute. Ring einschalten, falls er aus ist.",
    invalidCode: "Das ist kein Kopplungscode. Er hat 20 Zeichen, zum Beispiel ABCDE-12345-FGHJK-67890.",
    pairFailed: "Koppeln hat nicht geklappt: {e}",
    noTags: "Noch kein Tag gescannt. Halte einen Ring an einen Tag, dann erscheint er hier.",
    noAutomation: "keine Automation",
    createAutomation: "Anlegen",
    off: "aus",
    manageTags: "Tags verwalten",
    ringHint: "Scans eines deaktivierten Rings werden ignoriert. Ein gelöschter Ring braucht seinen Code erneut. Zum Umbenennen den Namen anklicken.",
    actions: "Aktionen",
    deactivate: "Deaktivieren",
    activate: "Aktivieren",
    remove: "Löschen",
    removeSure: "Wirklich löschen?",
    removed: "Der Ring ist gelöscht. Um ihn wieder zu nutzen, seinen Code eingeben.",
    removeReset: "Löschen und Ring zurücksetzen",
    resetHint: "Der Ring vergisst zusätzlich das Thread-Netz und Home Assistant und startet mit Werkseinstellungen neu. Danach muss er neu eingerichtet werden. Sein Code bleibt gleich.",
    removedReset: "Der Ring ist gelöscht und auf Werkseinstellungen zurückgesetzt. Er muss neu eingerichtet werden, bevor er wieder gekoppelt werden kann.",
    resetFailed: "Der Ring wurde nicht zurückgesetzt und nicht gelöscht: {e}",
    actionFailed: "Das hat nicht geklappt: {e}",
    deletedRing: "gelöschter Ring",
    error: "Die Übersicht konnte nicht geladen werden.",
    radio: "Funkart",
    radioThread: "Thread",
    radioBluetooth: "Bluetooth",
    radioZigbee: "Zigbee",
    radioZigbeeSoon: "Zigbee (noch nicht verfügbar)",
    radioWaitsContact: "Der Ring wechselt bei seinem nächsten Kontakt.",
    radioWaitsUpdate: "Firmware aktualisieren, danach wechselt der Ring von selbst.",
    radioViaBluetooth: "Noch nicht im Thread-Netz, über Bluetooth erreichbar.",
    radioFailed: "Die Funkart wurde nicht geändert: {e}",
    justNow: "gerade eben",
  },
};

const WATCHED = /^(tag|automation|update)\./;

// The pairing code in what a scanner read: the code alone, or at the end of
// a longer text such as a link. Home Assistant checks it properly.
const codeIn = (text) => {
  const found = /(?:^|[^0-9A-Za-z])((?:[0-9A-Za-z]{5}[- ]?){3}[0-9A-Za-z]{5})$/.exec(String(text ?? "").trim());
  return found ? found[1] : null;
};

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
    this._pairMsg = null; // {kind: "ok" | "info" | "err", text}
    this._pairBusy = false;
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
    this._scanStop?.();
    this._scanStop = null;
    clearInterval(this._timer);
    clearTimeout(this._poll);
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
    const t = this._t;
    if (this._pairMsg?.text === t.waiting && this._data && !this._data.waiting) {
      // The code found its ring, or ran out.
      this._pairMsg = this._data.rings.length > this._ringsBefore ? { kind: "ok", text: t.pairedNow } : null;
    }
    const pairing = !!this._data?.rings.some((r) => r.pairing);
    if (this._pairMsg?.text === t.pairingNow && this._data && !pairing) {
      // The ring has its key.
      this._pairMsg = { kind: "ok", text: t.pairedNow };
    }
    this._render();
    clearTimeout(this._poll);
    // New rings and a waiting code change without any entity changing.
    if (this._data?.waiting || pairing) this._poll = setTimeout(() => this._load(), 3000);
  }

  async _pair() {
    const t = this._t;
    const input = this.shadowRoot.querySelector("#code");
    const code = (input?.value || "").trim();
    if (!code || this._pairBusy) return;
    this._pairBusy = true;
    this._ringsBefore = this._data?.rings.length ?? 0;
    try {
      const result = await this._hass.callWS({ type: "nfc_ring/pair", code });
      input.value = "";
      this._pairMsg =
        result.status === "paired"
          ? { kind: "ok", text: t.pairedNow }
          : { kind: "info", text: result.status === "pairing" ? t.pairingNow : t.waiting };
    } catch (err) {
      this._pairMsg = {
        kind: "err",
        text: err?.code === "invalid_code" ? t.invalidCode : t.pairFailed.replace("{e}", err?.message || "?"),
      };
    } finally {
      this._pairBusy = false;
    }
    await this._load();
  }

  // The companion apps bring a scanner the page may open; a plain browser
  // has none, so the button is only shown inside the app.
  get _scanner() {
    const external = this._hass?.auth?.external;
    return external?.config?.hasBarCodeScanner ? external : null;
  }

  _scan() {
    const t = this._t;
    const external = this._scanner;
    if (!external || this._scanStop) return;
    const stop = () => {
      this._scanStop?.();
      this._scanStop = null;
    };
    this._scanStop = external.addCommandHandler((msg) => {
      if (msg.command === "bar_code/aborted") {
        stop();
        if (msg.payload?.reason === "alternative_options") this.shadowRoot.querySelector("#code")?.focus();
        return true;
      }
      if (msg.command !== "bar_code/scan_result") return false;
      const code = codeIn(msg.payload?.rawValue);
      if (!code) {
        external.fireMessage({ type: "bar_code/notify", payload: { message: t.scanWrong } });
        return true;
      }
      stop();
      external.fireMessage({ type: "bar_code/close" });
      const input = this.shadowRoot.querySelector("#code");
      if (input) input.value = code;
      this._pair();
      return true;
    });
    external.fireMessage({
      type: "bar_code/scan",
      payload: { title: t.scanTitle, description: t.scanHint, alternative_option_label: t.scanManual },
    });
  }

  async _setRadio(dev, radio) {
    const t = this._t;
    if (this._radioBusy) return;
    this._radioBusy = true;
    this._radioMsg = null;
    try {
      await this._hass.callWS({ type: "nfc_ring/set_radio", dev, radio });
    } catch (err) {
      this._radioMsg = { kind: "err", text: t.radioFailed.replace("{e}", err?.message || "?") };
    } finally {
      this._radioBusy = false;
    }
    await this._load();
  }

  // Deactivate, activate or delete a ring: what its device page does too.
  async _ringAction(id, action) {
    const t = this._t;
    const ring = this._data?.rings.find((r) => r.id === id);
    if (!ring || this._ringBusy) return;
    if (action === "remove" && this._sure !== id) {
      // First click only arms the button; it disarms by itself.
      this._sure = id;
      clearTimeout(this._sureTimer);
      this._sureTimer = setTimeout(() => {
        this._sure = null;
        this._render();
      }, 8000);
      this._render();
      return;
    }
    clearTimeout(this._sureTimer);
    this._sure = null;
    this._ringBusy = true;
    this._radioMsg = null;
    this._render();
    try {
      if (action === "reset") {
        // The ring has to confirm first; only then is it deleted here.
        try {
          await this._hass.callWS({ type: "nfc_ring/reset_ring", dev: ring.dev });
        } catch (err) {
          throw new Error(t.resetFailed.replace("{e}", err?.message || "?"));
        }
      }
      if (action === "remove" || action === "reset") {
        await this._hass.callWS({
          type: "config/device_registry/remove_config_entry",
          device_id: id,
          config_entry_id: ring.entry,
        });
        this._radioMsg = { kind: "ok", text: action === "reset" ? t.removedReset : t.removed };
      } else {
        await this._hass.callWS({
          type: "config/device_registry/update",
          device_id: id,
          disabled_by: action === "deactivate" ? "user" : null,
        });
      }
    } catch (err) {
      const own = action === "reset" && err instanceof Error && err.message.startsWith(t.resetFailed.split("{e}")[0]);
      this._radioMsg = { kind: "err", text: own ? err.message : t.actionFailed.replace("{e}", err?.message || "?") };
    } finally {
      this._ringBusy = false;
    }
    await this._load();
    this._render();
  }

  _radioCell(r) {
    const t = this._t;
    const names = { thread: t.radioThread, bluetooth: t.radioBluetooth, zigbee: t.radioZigbee };
    if (!r.radio) return `<span class="muted">–</span>`;
    let note = "";
    if (r.radio_waits === "update") note = t.radioWaitsUpdate;
    else if (r.radio_waits === "contact") note = t.radioWaitsContact;
    else if (r.radio === "thread" && r.link === "bluetooth") note = t.radioViaBluetooth;
    note = note ? `<div class="note">${note}</div>` : "";
    if (!r.paired || !this._hass?.user?.is_admin) return `${esc(names[r.radio] || r.radio)}${note}`;
    const options = (r.radios || [])
      .map((o) => `<option value="${esc(o)}"${o === r.radio ? " selected" : ""}>${esc(names[o] || o)}</option>`)
      .join("");
    const soon = (r.radios || []).includes("zigbee") ? "" : `<option disabled>${t.radioZigbeeSoon}</option>`;
    return `<select class="radio" data-dev="${esc(r.dev)}" aria-label="${t.radio}">${options}${soon}</select>${note}`;
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

  _ringRows(rings, pending) {
    const t = this._t;
    const known = rings.map((r) => {
      const status = r.disabled
        ? `<span class="pill off">${t.disabled}</span>`
        : `<span class="pill on">${t.active}</span>`;
      const pairing = r.paired
        ? `<span class="pill on">${t.paired}</span>`
        : r.pairing
        ? `<span class="pill warn">${t.pairingPill}</span><div class="note">${t.pairingHint}</div>`
        : `<span class="pill warn">${t.unpaired}</span><div class="note">${r.can_pair ? t.unpairedReady : t.unpairedHint}</div>`;
      const fw = r.update_available
        ? `${esc(r.firmware || "–")} <span class="pill warn">${esc(t.update.replace("{v}", r.latest_firmware || "?"))}</span>`
        : `${esc(r.firmware || "–")} <span class="muted">· ${t.upToDate}</span>`;
      return `<tr class="${r.disabled ? "dim" : ""}">
          <td data-l="${t.name}"><div><a href="/config/devices/device/${esc(r.id)}">${esc(r.name)}</a></div></td>
          <td data-l="${t.status}"><div>${status}</div></td>
          <td data-l="${t.pairing}"><div>${pairing}</div></td>
          <td data-l="${t.radio}"><div>${this._radioCell(r)}</div></td>
          <td data-l="${t.firmware}"><div>${fw}</div></td>
          <td data-l="${t.lastScan}"><div>${this._when(r.last_scan)}</div></td>
          <td data-l="${t.id}" class="mono"><div>${esc(r.dev || "–")}</div></td>
          <td data-l="${t.actions}"><div class="acts">
            <button type="button" class="act" data-id="${esc(r.id)}" data-act="${r.disabled ? "activate" : "deactivate"}">${r.disabled ? t.activate : t.deactivate}</button>
            <button type="button" class="act danger${this._sure === r.id ? " sure" : ""}" data-id="${esc(r.id)}" data-act="remove">${this._sure === r.id ? t.removeSure : t.remove}</button>
            ${this._sure === r.id && r.can_reset ? `<button type="button" class="act danger" data-id="${esc(r.id)}" data-act="reset" title="${esc(t.resetHint)}">${t.removeReset}</button>` : ""}
          </div></td>
        </tr>`;
    });
    // Rings that reported but are refused until their code is entered.
    const waiting = pending.map((p) => {
      const pairing = p.can_pair
        ? `<span class="pill warn">${t.needsCode}</span>`
        : `<span class="pill off">${t.tooOld}</span><div class="note">${t.tooOldHint}</div>`;
      return `<tr class="dim">
          <td data-l="${t.name}"><div>${esc(t.newRing.replace("{id}", p.dev.slice(-4).toUpperCase()))}</div></td>
          <td data-l="${t.status}"><div><span class="muted">–</span></div></td>
          <td data-l="${t.pairing}"><div>${pairing}</div></td>
          <td data-l="${t.radio}"><div><span class="muted">–</span></div></td>
          <td data-l="${t.firmware}"><div>${esc(p.firmware || "–")}</div></td>
          <td data-l="${t.lastScan}"><div><span class="muted">–</span></div></td>
          <td data-l="${t.id}" class="mono"><div>${esc(p.dev)}</div></td>
          <td data-l="${t.actions}"><div><span class="muted">–</span></div></td>
        </tr>`;
    });
    return known.concat(waiting).join("");
  }

  _pairForm() {
    const t = this._t;
    if (!this._hass?.user?.is_admin) return "";
    const msg = this._pairMsg ? `<p class="msg ${this._pairMsg.kind}">${esc(this._pairMsg.text)}</p>` : "";
    return `<form class="pair" autocomplete="off">
        <h3>${t.addRing}</h3>
        <p class="hint">${t.addHint}</p>
        <div class="row">
          <input id="code" type="text" inputmode="text" spellcheck="false" autocapitalize="characters"
                 maxlength="40" placeholder="XXXXX-XXXXX-XXXXX-XXXXX" aria-label="${t.addRing}">
          <button type="submit">${t.pair}</button>
          ${this._scanner ? `<button type="button" id="scan" class="second">${t.scan}</button>` : ""}
        </div>
        ${msg}
      </form>`;
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
        // A ring that was deleted since has no name and no page any more.
        const ring = !tag.device_id
          ? `<span class="muted">–</span>`
          : tag.device_name
            ? `<a href="/config/devices/device/${esc(tag.device_id)}">${esc(tag.device_name)}</a>`
            : `<span class="muted">${t.deletedRing}</span>`;
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
      const pending = data.pending || [];
      const rings = data.rings.length + pending.length
        ? `<div class="scroll"><table>
            <thead><tr><th>${t.name}</th><th>${t.status}</th><th>${t.pairing}</th><th>${t.radio}</th><th>${t.firmware}</th><th>${t.lastScan}</th><th>${t.id}</th><th>${t.actions}</th></tr></thead>
            <tbody>${this._ringRows(data.rings, pending)}</tbody></table></div>
           ${this._radioMsg ? `<p class="msg ${this._radioMsg.kind}">${esc(this._radioMsg.text)}</p>` : ""}
           ${data.rings.length ? `<p class="hint">${t.ringHint}</p>` : ""}`
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
          ${this._pairForm()}
        </section>
        <section class="card">
          <h2>${t.tags} <span class="count">${data.tags.length}</span>
            <a class="action" href="/config/tags">${t.manageTags}</a></h2>
          ${tags}
        </section>`;
    }
    // Rebuilding the page must not close a radio list that is open.
    if (this.shadowRoot.activeElement?.matches?.("select.radio") && !this._radioBusy) {
      this._renderLater = true;
      return;
    }
    this._renderLater = false;
    // Rebuilding the page must not eat a code that is being typed.
    const before = this.shadowRoot.querySelector("#code");
    const typed = before?.value || "";
    const focused = before && this.shadowRoot.activeElement === before;
    const caret = focused ? [before.selectionStart, before.selectionEnd] : null;
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="bar"><ha-menu-button></ha-menu-button><h1>${t.title}</h1></div>
      <main>${body}</main>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    const input = this.shadowRoot.querySelector("#code");
    if (input) {
      input.value = typed;
      if (focused) {
        input.focus();
        input.setSelectionRange(...caret);
      }
      this.shadowRoot.querySelector("form.pair").addEventListener("submit", (ev) => {
        ev.preventDefault();
        this._pair();
      });
      this.shadowRoot.querySelector("#scan")?.addEventListener("click", () => this._scan());
    }
    this.shadowRoot.querySelectorAll("button.act").forEach((button) =>
      button.addEventListener("click", () => this._ringAction(button.dataset.id, button.dataset.act))
    );
    this.shadowRoot.querySelectorAll("select.radio").forEach((select) => {
      select.addEventListener("change", () => {
        select.blur();
        this._setRadio(select.dataset.dev, select.value);
      });
      select.addEventListener("blur", () => {
        if (this._renderLater) this._render();
      });
    });
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
  .note { color: var(--secondary-text-color); font-size: 12px; line-height: 1.4; margin-top: 4px; max-width: 26em; }
  .pair { border-top: 1px solid var(--divider-color, #e0e0e0); margin-top: 12px; padding-top: 4px; }
  .pair h3 { font-size: 15px; font-weight: 500; margin: 0; padding: 12px 20px 0; }
  .pair .hint { padding-top: 4px; }
  .pair .row { display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 20px 4px; }
  .pair input { flex: 1 1 16em; max-width: 22em; min-width: 0; box-sizing: border-box; height: 40px; padding: 0 12px;
                font: inherit; font-family: var(--ha-font-family-code, ui-monospace, Menlo, Consolas, monospace); letter-spacing: .04em;
                color: var(--primary-text-color); background: var(--secondary-background-color);
                border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; }
  .pair input:focus-visible, .pair button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  .pair button { height: 40px; padding: 0 18px; font: inherit; font-weight: 500; cursor: pointer; border: none; border-radius: 8px;
                 color: var(--text-primary-color, #fff); background: var(--primary-color); }
  .pair button.second { color: var(--primary-color); background: none; border: 1px solid var(--primary-color); }
  .acts { display: flex; flex-wrap: wrap; gap: 6px; }
  button.act { font: inherit; font-size: 13px; font-weight: 500; height: 30px; padding: 0 10px; cursor: pointer; white-space: nowrap;
               color: var(--primary-color); background: none; border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; }
  button.act.danger { color: var(--error-color, #c62828); }
  button.act.sure { color: #fff; background: var(--error-color, #c62828); border-color: var(--error-color, #c62828); }
  button.act:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  select.radio { font: inherit; height: 32px; padding: 0 6px; color: var(--primary-text-color);
                 background: var(--secondary-background-color); border: 1px solid var(--divider-color, #e0e0e0); border-radius: 8px; }
  select.radio:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  .msg { font-size: 14px; margin: 0; padding: 8px 20px 4px; }
  .msg.ok { color: var(--success-color, #2e7d32); }
  .msg.err { color: var(--error-color, #c62828); }
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
