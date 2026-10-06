from __future__ import annotations

import argparse
import hmac
import http.client
import json
import math
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .analytics import (
    ANALYTICS_DIMENSIONS,
    ANALYTICS_METRICS,
    REACTION_METRICS,
    AnalyticsContract,
    AnalyticsService,
    ad_metric_ranking_to_dict,
    analytics_contract_to_dict,
    group_metric_ranking_to_dict,
)
from .query import (
    MarkQueryService,
    ad_snapshot_to_dict,
    ad_view_to_dict,
    email_reaction_view_to_dict,
    reaction_snapshot_to_dict,
    summary_to_dict,
)
from .storage import SnapshotStore


_MAX_PROXY_JSON_BODY_BYTES = 16 * 1024
_MAX_PROXY_MEDIA_BODY_BYTES = 25 * 1024 * 1024
_MAX_PROXY_RESPONSE_BYTES = 256 * 1024
_PROXY_BODY_READ_TIMEOUT_SECONDS = 10.0
_DASHBOARD_IDEMPOTENCY_KEY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
_DASHBOARD_WRITE_MARKER_HEADER = "X-Mark-Dashboard-Write"
_DASHBOARD_WRITE_MARKER_VALUE = "1"
_DASHBOARD_WRITE_TOKEN_HEADER = "X-Mark-Dashboard-Token"


def _dashboard_http_origin(host: str, port: int) -> str:
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


@dataclass(frozen=True, slots=True)
class DashboardWriteProxy:
    """Narrow same-origin gateway to the existing loopback Write API."""

    host: str
    port: int
    bearer_token: str = field(repr=False)
    ui_token: str = field(repr=False)
    timeout_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("dashboard write proxy must target 127.0.0.1")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("dashboard write proxy port must be 1..65535")
        for name, value in (
            ("bearer_token", self.bearer_token),
            ("ui_token", self.ui_token),
        ):
            if (
                not isinstance(value, str)
                or len(value) < 16
                or len(value) > 512
                or any(character.isspace() for character in value)
            ):
                raise ValueError(f"{name} is invalid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("dashboard write proxy timeout must be positive")


_DASHBOARD_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mark Dashboard</title>
  <link rel="stylesheet" href="/dashboard.css">
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Mark Dashboard</h1>
        <p>Lokale Sicht auf Historie, Analytics und sichere Produktaktionen.</p>
      </div>
      <button id="reload" type="button">Neu laden</button>
    </header>

    <section id="status" class="status" aria-live="polite"></section>

    <section class="cards" aria-label="Zusammenfassung">
      <article><span>Bekannt</span><strong id="tracked">—</strong></article>
      <article><span>Aktuell</span><strong id="current">—</strong></article>
      <article><span>Entfernt</span><strong id="absent">—</strong></article>
      <article><span>Views (bekannt)</span><strong id="views">—</strong></article>
      <article><span>Merker (bekannt)</span><strong id="watches">—</strong></article>
      <article><span>Replies (bekannt)</span><strong id="replies">—</strong></article>
    </section>

    <section id="write-panel" class="panel" hidden>
      <h2>Anzeigen verwalten</h2>
      <p class="muted">Schreibaktionen laufen ausschließlich über die bestehende lokale Write API mit denselben Idempotenz-, Ownership-, Confirmation- und No-Blind-Retry-Grenzen.</p>
      <p id="write-session-note" class="note" hidden>Für Schreibaktionen diese Seite über die vom Product Launcher ausgegebene Dashboard-URL öffnen. Ohne per-process Write-Token bleibt die Oberfläche read-only.</p>
      <p id="write-status" class="status write-status" aria-live="polite"></p>
      <div class="write-grid">
        <form id="create-form" class="write-form">
          <h3>Neue Anzeige</h3>
          <label>
            Kategoriepfad
            <input id="create-category" data-write-control type="text" placeholder="Haus &amp; Garten &gt; Dekoration" autocomplete="off">
          </label>
          <label>
            Titel
            <input id="create-title" data-write-control type="text" autocomplete="off">
          </label>
          <label>
            Beschreibung
            <textarea id="create-description" data-write-control></textarea>
          </label>
          <label>
            Festpreis in Euro
            <input id="create-price" data-write-control type="number" min="1" step="1" inputmode="numeric">
          </label>
          <label>
            Bilder (optional)
            <input id="create-media" data-write-control type="file" accept="image/jpeg,image/png,image/webp" multiple>
          </label>
          <div class="write-actions">
            <button id="create-submit" data-write-control type="submit">Anzeige erstellen</button>
          </div>
        </form>

        <form id="manage-form" class="write-form">
          <h3>Bestehende Anzeige</h3>
          <label>
            Anzeigen-ID
            <input id="manage-ad-id" type="text" readonly>
          </label>
          <p class="note">Aktueller lokaler Status: <strong id="manage-state">—</strong></p>
          <label>
            Titel
            <input id="manage-title" data-write-control type="text" autocomplete="off">
          </label>
          <label>
            Beschreibung
            <textarea id="manage-description" data-write-control></textarea>
          </label>
          <div class="write-actions">
            <button id="manage-save" data-write-control type="button">Titel/Beschreibung speichern</button>
            <button id="manage-pause" data-write-control type="button">Pausieren</button>
            <button id="manage-activate" data-write-control type="button">Aktivieren</button>
            <button id="manage-delete" data-write-control class="danger" type="button">Löschen</button>
          </div>
        </form>
      </div>
    </section>

    <section id="write-unavailable" class="panel" hidden>
      <h2>Anzeigen verwalten</h2>
      <p class="muted">Diese standalone Dashboard-Instanz ist read-only. Der Product Launcher aktiviert die sichere Write-UX separat.</p>
    </section>

    <section class="panel">
      <h2>Analytics-Rankings</h2>
      <p class="muted">Rohmetriken nach explizit gespeicherten Labels. Keine Qualitäts- oder Kausalaussage.</p>
      <p id="analytics-contract" class="note"></p>
      <div class="controls">
        <label>
          Metrik
          <select id="metric-select"></select>
        </label>
        <label>
          Dimension
          <select id="dimension-select"></select>
        </label>
      </div>

      <h3>Gruppen</h3>
      <div id="groups-chart" class="chart" role="list" aria-label="Gruppenvergleich nach Mittelwert" hidden></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Label</th>
              <th>Stichprobe</th>
              <th>Summe</th>
              <th>Mittelwert</th>
            </tr>
          </thead>
          <tbody id="groups-body"></tbody>
        </table>
      </div>
      <p id="groups-empty" class="empty" hidden>Keine ausreichenden klassifizierten Daten für diese Auswahl.</p>
      <p class="note">Stichprobengröße 1 ist nur ein Datenwert, keine Qualitätsaussage.</p>

      <h3>Anzeigenranking</h3>
      <div id="ranking-chart" class="chart" role="list" aria-label="Anzeigenvergleich nach ausgewählter Metrik" hidden></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Titel</th>
              <th>Wert</th>
              <th>Vorhanden</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="ranking-body"></tbody>
        </table>
      </div>
      <p id="ranking-empty" class="empty" hidden>Keine Anzeigen mit bekanntem Wert für diese Metrik.</p>
    </section>

    <section class="panel">
      <h2>Anzeigen</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Titel</th>
              <th>Status</th>
              <th>Views</th>
              <th>Merker</th>
              <th>Replies</th>
              <th>Letzte Beobachtung</th>
              <th>Aktion</th>
            </tr>
          </thead>
          <tbody id="ads-body"></tbody>
        </table>
      </div>
      <p id="empty" class="empty" hidden>Keine Anzeigenhistorie vorhanden.</p>
    </section>
  </main>
  <script src="/dashboard.js" defer></script>
</body>
</html>
"""

_DASHBOARD_CSS = """
:root {
  color-scheme: light dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
  line-height: 1.45;
}
body {
  margin: 0;
  background: #111318;
  color: #f4f5f7;
}
main {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px 20px 56px;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 22px;
}
h1, h2, h3, p { margin-top: 0; }
header p, .muted, .note { color: #aeb5c0; }
header p { margin-bottom: 0; }
button, select, input, textarea {
  border: 1px solid #424a57;
  background: #20252d;
  color: inherit;
  border-radius: 8px;
  padding: 9px 14px;
  box-sizing: border-box;
}
button { cursor: pointer; }
button:hover { background: #2a313b; }
button:disabled { cursor: not-allowed; opacity: 0.55; }
textarea { min-height: 110px; resize: vertical; }
.status {
  min-height: 1.4em;
  color: #f2c36b;
  margin-bottom: 12px;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 22px;
}
.cards article, .panel {
  border: 1px solid #303641;
  border-radius: 12px;
  background: #181c22;
}
.cards article { padding: 16px; }
.cards span {
  display: block;
  color: #9da6b2;
  font-size: 0.9rem;
}
.cards strong {
  display: block;
  margin-top: 5px;
  font-size: 1.7rem;
}
.panel {
  padding: 18px;
  margin-bottom: 22px;
}
.write-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 18px;
}
.write-form {
  display: grid;
  gap: 12px;
  align-content: start;
  border: 1px solid #2d333d;
  border-radius: 10px;
  padding: 14px;
}
.write-form label {
  display: grid;
  gap: 6px;
  color: #b7bec8;
  font-size: 0.9rem;
}
.write-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
button.danger { border-color: #87505a; }
.write-status.success { color: #82db9e; }
.write-status.warning { color: #f2c36b; }
.write-status.error { color: #ef9aa8; }
.row-action { padding: 6px 10px; }
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 14px 0 20px;
}
.controls label {
  display: grid;
  gap: 6px;
  color: #b7bec8;
  font-size: 0.9rem;
}
.chart {
  display: grid;
  gap: 8px;
  margin: 10px 0 16px;
}
.chart[hidden] { display: none; }
.chart-row {
  display: grid;
  grid-template-columns: minmax(90px, 180px) minmax(120px, 1fr) auto;
  align-items: center;
  gap: 10px;
}
.chart-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #c8ced7;
}
.chart-progress {
  width: 100%;
  height: 12px;
  border: 0;
  border-radius: 999px;
  overflow: hidden;
  appearance: none;
  -webkit-appearance: none;
  background: #252b34;
}
.chart-progress::-webkit-progress-bar {
  background: #252b34;
  border-radius: 999px;
}
.chart-progress::-webkit-progress-value {
  background: #8ca6cf;
  border-radius: 999px;
}
.chart-progress::-moz-progress-bar {
  background: #8ca6cf;
  border-radius: 999px;
}
.chart-value {
  min-width: 7.5em;
  text-align: right;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  color: #b7bec8;
}
.table-wrap { overflow-x: auto; }
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  padding: 10px 9px;
  border-bottom: 1px solid #2d333d;
  text-align: left;
  white-space: nowrap;
}
th { color: #b7bec8; font-size: 0.86rem; }
td.title {
  min-width: 220px;
  white-space: normal;
}
.state {
  display: inline-block;
  border-radius: 999px;
  padding: 3px 8px;
  background: #252b34;
}
.state.active { color: #82db9e; }
.state.paused, .state.pending { color: #f2c36b; }
.state.absent { color: #aeb5c0; }
.empty { color: #9da6b2; margin-bottom: 0; }
.note { margin: 10px 0 22px; font-size: 0.88rem; }
@media (max-width: 640px) {
  header { align-items: flex-start; flex-direction: column; }
  .chart-row { grid-template-columns: minmax(70px, 110px) minmax(0, 1fr); }
  .chart-value {
    grid-column: 2;
    min-width: 0;
    white-space: normal;
    overflow-wrap: anywhere;
  }
}
"""

_DASHBOARD_JS = """
const byId = (id) => document.getElementById(id);

const display = (value) => value === null || value === undefined ? "—" : String(value);
const WRITE_TOKEN_STORAGE_KEY = "mark-dashboard-write-token";
const pendingWrites = new Map();
let writeUiAvailable = false;
let writeToken = null;
let pendingRecoveryBlocked = false;
let createMediaInFlight = false;
let latestAdsById = new Map();
let managedAdBaseline = null;

function consumeWriteToken() {
  const hashParams = new URLSearchParams(window.location.hash.slice(1));
  const candidate = hashParams.get("write_token");
  if (
    candidate
    && candidate.length >= 16
    && candidate.length <= 512
    && !/\\s/.test(candidate)
  ) {
    writeToken = candidate;
    try {
      window.sessionStorage.setItem(WRITE_TOKEN_STORAGE_KEY, candidate);
    } catch (_error) {
      // In-memory token remains usable for this page.
    }
  } else {
    try {
      writeToken = window.sessionStorage.getItem(WRITE_TOKEN_STORAGE_KEY);
    } catch (_error) {
      writeToken = null;
    }
  }
  if (window.location.hash) {
    window.history.replaceState(
      null,
      "",
      window.location.pathname + window.location.search,
    );
  }
}

function writeUiReady() {
  return (
    writeUiAvailable
    && typeof writeToken === "string"
    && writeToken.length >= 16
    && !pendingRecoveryBlocked
  );
}

function validPendingEntry(entry) {
  if (!entry || typeof entry !== "object") return false;
  if (
    typeof entry.scope !== "string"
    || typeof entry.key !== "string"
    || typeof entry.method !== "string"
    || typeof entry.path !== "string"
  ) {
    return false;
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(entry.key)) {
    return false;
  }
  if (!["POST", "PATCH", "DELETE"].includes(entry.method)) return false;
  if (!entry.path.startsWith("/api/write/")) return false;
  if (
    entry.payload !== null
    && (
      typeof entry.payload !== "object"
      || Array.isArray(entry.payload)
    )
  ) {
    return false;
  }
  if (
    entry.adId !== null
    && entry.adId !== undefined
    && (typeof entry.adId !== "string" || !/^[0-9]{1,32}$/.test(entry.adId))
  ) {
    return false;
  }
  if (
    entry.acknowledged !== undefined
    && typeof entry.acknowledged !== "boolean"
  ) {
    return false;
  }
  return true;
}

async function acknowledgePendingWrite(entry) {
  const response = await fetch("/api/dashboard/pending-writes/ack", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Mark-Dashboard-Write": "1",
      "X-Mark-Dashboard-Token": writeToken,
    },
    body: JSON.stringify({
      scope: entry.scope,
      idempotency_key: entry.key,
    }),
    cache: "no-store",
  });
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_error) {
    throw new Error("pending recovery acknowledgement invalid");
  }
  if (!response.ok || payload.acknowledged !== true) {
    throw new Error(payload.error ?? "pending recovery acknowledgement failed");
  }
}

async function refreshPendingWrites() {
  pendingWrites.clear();
  pendingRecoveryBlocked = false;
  if (
    !writeUiAvailable
    || typeof writeToken !== "string"
    || writeToken.length < 16
  ) {
    return;
  }
  try {
    const response = await fetch("/api/dashboard/pending-writes", {
      headers: {
        "X-Mark-Dashboard-Write": "1",
        "X-Mark-Dashboard-Token": writeToken,
      },
      cache: "no-store",
    });
    const text = await response.text();
    const payload = text ? JSON.parse(text) : {};
    if (
      !response.ok
      || !payload
      || !Array.isArray(payload.pending_writes)
      || payload.pending_writes.some((entry) => !validPendingEntry(entry))
    ) {
      throw new Error("pending recovery unavailable");
    }
    const acknowledgedEntries = [];
    for (const entry of payload.pending_writes) {
      if (entry.acknowledged === true) {
        acknowledgedEntries.push(entry);
        continue;
      }
      if (pendingWrites.has(entry.scope)) {
        throw new Error("duplicate pending scope");
      }
      pendingWrites.set(entry.scope, entry);
    }
    for (const entry of acknowledgedEntries) {
      try {
        await acknowledgePendingWrite(entry);
      } catch (_error) {
        // This GET already proved that a previous browser observed the terminal
        // write result and durably acknowledged it. If tombstone cleanup did not
        // reach the server, its retained row still blocks a fresh platform key.
      }
    }
  } catch (_error) {
    pendingWrites.clear();
    pendingRecoveryBlocked = true;
  }
}

function setWriteStatus(message, kind = "warning") {
  const status = byId("write-status");
  status.textContent = message;
  status.className = `status write-status ${kind}`;
}

function renderWriteAvailability() {
  byId("write-panel").hidden = !writeUiAvailable;
  byId("write-unavailable").hidden = writeUiAvailable;
  if (!writeUiAvailable) return;
  if (pendingRecoveryBlocked) {
    byId("write-session-note").hidden = true;
    for (const element of document.querySelectorAll("[data-write-control]")) {
      element.disabled = true;
    }
    setWriteStatus(
      "Der persistente Recovery-Status früherer Write-Anfragen ist nicht sicher lesbar. Neue Writes bleiben fail-closed gesperrt.",
      "error",
    );
    return;
  }
  const ready = writeUiReady();
  byId("write-session-note").hidden = ready;
  for (const element of document.querySelectorAll("[data-write-control]")) {
    element.disabled = !ready;
  }
  if (ready && pendingWrites.size > 0) {
    setWriteStatus(
      "Eine frühere Write-Anfrage bleibt mit ihrem ursprünglichen Idempotency-Key gebunden. Eine manuelle Wiederholung sendet exakt denselben Request; es erfolgt kein automatischer Retry.",
      "warning",
    );
  }
}

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function td(value, className = "") {
  const element = document.createElement("td");
  element.textContent = display(value);
  if (className) element.className = className;
  return element;
}

function managedBaselineFor(ad) {
  const titleKnown = typeof ad.title === "string";
  const descriptionKnown = typeof ad.description === "string";
  return {
    adId: ad.ad_id,
    titleKnown,
    title: titleKnown ? ad.title : null,
    descriptionKnown,
    description: descriptionKnown ? ad.description : null,
  };
}

function populateManageForm(ad) {
  managedAdBaseline = managedBaselineFor(ad);
  byId("manage-ad-id").value = ad.ad_id;
  byId("manage-title").value = managedAdBaseline.titleKnown ? managedAdBaseline.title : "";
  byId("manage-description").value = managedAdBaseline.descriptionKnown
    ? managedAdBaseline.description
    : "";
  byId("manage-state").textContent = ad.lifecycle_state ?? "—";
  byId("manage-form").scrollIntoView({behavior: "smooth", block: "nearest"});
}

function rebaseManagedBaseline(adId, payload) {
  if (byId("manage-ad-id").value !== adId) return;
  if (managedAdBaseline === null || managedAdBaseline.adId !== adId) return;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) return;

  const next = {...managedAdBaseline};
  if (
    Object.prototype.hasOwnProperty.call(payload, "title")
    && typeof payload.title === "string"
  ) {
    next.titleKnown = true;
    next.title = payload.title;
  }
  if (
    Object.prototype.hasOwnProperty.call(payload, "description")
    && typeof payload.description === "string"
  ) {
    next.descriptionKnown = true;
    next.description = payload.description;
  }
  managedAdBaseline = next;
}

function managedContentChanges(adId) {
  if (managedAdBaseline === null || managedAdBaseline.adId !== adId) {
    throw new Error("Management-Ausgangswerte sind nicht mehr eindeutig gebunden.");
  }
  const payload = {};
  const title = byId("manage-title").value;
  const description = byId("manage-description").value;
  if (
    managedAdBaseline.titleKnown
      ? title !== managedAdBaseline.title
      : title !== ""
  ) {
    payload.title = title;
  }
  if (
    managedAdBaseline.descriptionKnown
      ? description !== managedAdBaseline.description
      : description !== ""
  ) {
    payload.description = description;
  }
  return payload;
}

function renderAds(ads) {
  latestAdsById = new Map(ads.map((ad) => [ad.ad_id, ad]));
  const body = byId("ads-body");
  body.replaceChildren();
  byId("empty").hidden = ads.length !== 0;

  for (const ad of ads) {
    const row = document.createElement("tr");
    row.append(td(ad.ad_id));
    row.append(td(ad.title, "title"));

    const stateCell = document.createElement("td");
    const state = document.createElement("span");
    state.className = `state ${ad.lifecycle_state}`;
    state.textContent = ad.lifecycle_state;
    stateCell.append(state);
    row.append(stateCell);

    row.append(td(ad.views));
    row.append(td(ad.watch_count));
    row.append(td(ad.reply_count));
    row.append(td(ad.observed_at));

    const actionCell = document.createElement("td");
    const pending = pendingForAd(ad.ad_id);
    if (writeUiReady() && (ad.present || pending !== null)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "row-action";
      button.textContent = ad.present ? "Verwalten" : "Recovery";
      button.addEventListener("click", () => populateManageForm(ad));
      actionCell.append(button);
    }
    row.append(actionCell);
    body.append(row);
  }
}

function setOptions(select, values, placeholder = null) {
  select.replaceChildren();
  if (placeholder !== null) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = placeholder;
    select.append(option);
  }
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
}

function renderBarChart(containerId, items, valueOf, labelOf, formatValue) {
  const container = byId(containerId);
  container.replaceChildren();
  const rows = [];
  for (const item of items) {
    const rawValue = valueOf(item);
    if (rawValue === null || rawValue === undefined) continue;
    const value = Number(rawValue);
    if (!Number.isFinite(value) || value < 0) continue;
    rows.push({item, value});
  }
  container.hidden = rows.length === 0;
  if (rows.length === 0) return;

  const max = Math.max(...rows.map((entry) => entry.value), 0);
  for (const entry of rows) {
    const row = document.createElement("div");
    row.className = "chart-row";
    row.setAttribute("role", "listitem");

    const label = document.createElement("span");
    label.className = "chart-label";
    label.textContent = String(labelOf(entry.item));

    const progress = document.createElement("progress");
    progress.className = "chart-progress";
    progress.max = max === 0 ? 1 : max;
    progress.value = entry.value;
    progress.setAttribute("aria-hidden", "true");

    const value = document.createElement("span");
    value.className = "chart-value";
    value.textContent = formatValue(entry.value, entry.item);

    row.append(label, progress, value);
    container.append(row);
  }
}

function renderGroups(groups) {
  const body = byId("groups-body");
  body.replaceChildren();
  byId("groups-empty").hidden = groups.length !== 0;
  for (const group of groups) {
    const row = document.createElement("tr");
    row.append(td(group.label));
    row.append(td(group.sample_size));
    row.append(td(group.metric_sum));
    row.append(td(Number(group.metric_mean).toFixed(2)));
    body.append(row);
  }
  renderBarChart(
    "groups-chart",
    groups,
    (group) => group.metric_mean,
    (group) => group.label,
    (value, group) => `${value.toFixed(2)} (n=${group.sample_size})`,
  );
}

let analyticsRequestGeneration = 0;

function renderAnalyticsContract(contract) {
  const reaction = contract.reaction_metric ?? "nicht festgelegt";
  const objective = contract.objective_metric ?? "nicht festgelegt";
  byId("analytics-contract").textContent =
    `Reaktionsmetrik: ${reaction}; Optimierungsziel: ${objective}. Rankings werden nur nach explizit ausgewählter Metrik geladen.`;
}

function renderRanking(items) {
  const body = byId("ranking-body");
  body.replaceChildren();
  byId("ranking-empty").hidden = items.length !== 0;
  for (const item of items) {
    const row = document.createElement("tr");
    row.append(td(item.ad_id));
    row.append(td(item.title, "title"));
    row.append(td(item.value));
    row.append(td(item.present === null ? "—" : (item.present ? "ja" : "nein")));
    row.append(td(item.lifecycle_state));
    body.append(row);
  }
  renderBarChart(
    "ranking-chart",
    items,
    (item) => item.value,
    (item) => item.ad_id,
    (value) => String(value),
  );
}

function newIdempotencyKey() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return `ui:${globalThis.crypto.randomUUID()}`;
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return "ui:" + Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function pendingForAd(adId) {
  for (const entry of pendingWrites.values()) {
    if (entry.adId === adId) return entry;
  }
  return null;
}

async function proxyRequest(method, path, {body = null, contentType = null, filename = null, idempotencyKey = null} = {}) {
  if (!writeUiReady()) {
    throw new Error("Write-Sitzung ist nicht aktiv.");
  }
  const headers = {
    "X-Mark-Dashboard-Write": "1",
    "X-Mark-Dashboard-Token": writeToken,
  };
  if (contentType !== null) headers["Content-Type"] = contentType;
  if (filename !== null) headers["X-Mark-Media-Filename"] = filename;
  if (idempotencyKey !== null) headers["Idempotency-Key"] = idempotencyKey;

  const response = await fetch(path, {
    method,
    headers,
    body,
    cache: "no-store",
  });
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_error) {
    throw new Error("Ungültige Antwort der lokalen Write API.");
  }
  return {response, payload};
}

function completedWriteResponse(entry, payload) {
  return payload && payload.idempotency_key === entry.key;
}

const DETERMINISTIC_PRE_EXECUTION_ERRORS = new Set([
  "invalid_create_request",
  "invalid_media_create_request",
  "invalid_media_refs",
  "invalid_content_fields",
  "title_must_be_string",
  "description_must_be_string",
  "invalid_unicode_text",
]);

function canClearPendingAfterError(response, payload, retryingPending = false) {
  if (!payload || typeof payload.error !== "string") return false;
  if (
    payload.error === "idempotency_in_progress"
    || payload.error === "idempotency_conflict"
    || payload.error === "write_proxy_transport_unknown"
  ) {
    return false;
  }
  if (response.status < 400 || response.status >= 500) return false;
  if (!retryingPending) return true;
  return (
    response.status === 400
    && DETERMINISTIC_PRE_EXECUTION_ERRORS.has(payload.error)
  );
}

function canClearCompletedWrite(_response, _payload) {
  // completedWriteResponse already established the exact persisted
  // Idempotency-Key returned by the Write API.
  return true;
}

function confirmedWriteResult(result) {
  return (
    result !== null
    && result.response.status === 200
    && result.payload?.operation_receipt?.outcome === "confirmed"
  );
}

async function refreshAfterSettledWrite() {
  try {
    await load();
  } catch (error) {
    setWriteStatus(
      `Write-Ergebnis ist geklärt, aber Dashboard-Aktualisierung fehlgeschlagen: ${error.message}`,
      "warning",
    );
  }
}

function describeWriteResult(response, payload) {
  if (payload.operation_receipt) {
    const outcome = payload.operation_receipt.outcome ?? "unknown";
    if (response.status === 200) {
      return {kind: "success", text: `Bestätigt: ${outcome}.`};
    }
    if (response.status === 202) {
      return {
        kind: "warning",
        text: `Ausgang unklar (${outcome}). Kein automatischer Plattform-Retry.`,
      };
    }
    return {kind: "error", text: `Write-Ergebnis: ${outcome} (HTTP ${response.status}).`};
  }
  if (payload.error) {
    const retry = payload.platform_retry_authorized === true
      ? " Retry autorisiert."
      : " Kein automatischer Plattform-Retry.";
    return {kind: response.ok ? "warning" : "error", text: `${payload.error} (HTTP ${response.status}).${retry}`};
  }
  return {kind: response.ok ? "success" : "error", text: `HTTP ${response.status}.`};
}

async function runPlatformWrite(scope, method, path, payload, {adId = null} = {}) {
  let entry = pendingWrites.get(scope);
  const retryingPending = entry !== undefined;
  if (entry === undefined) {
    entry = {
      scope,
      key: newIdempotencyKey(),
      method,
      path,
      payload,
      adId,
    };
    pendingWrites.set(scope, entry);
  }

  try {
    const body = entry.payload === null ? null : JSON.stringify(entry.payload);
    const {response, payload: responsePayload} = await proxyRequest(
      entry.method,
      entry.path,
      {
        body,
        contentType: entry.payload === null ? null : "application/json",
        idempotencyKey: entry.key,
      },
    );
    const result = describeWriteResult(response, responsePayload);
    setWriteStatus(result.text, result.kind);

    if (completedWriteResponse(entry, responsePayload)) {
      if (canClearCompletedWrite(response, responsePayload)) {
        try {
          await acknowledgePendingWrite(entry);
        } catch (error) {
          setWriteStatus(
            `Write-Ergebnis ist terminal, aber der lokale Recovery-ACK ist fehlgeschlagen: ${error.message}. Derselbe Request/Key bleibt gebunden.`,
            "warning",
          );
          return {response, payload: responsePayload};
        }
        pendingWrites.delete(scope);
      }
      await refreshAfterSettledWrite();
    } else if (responsePayload.error === "dashboard_pending_write_conflict") {
      await refreshAfterSettledWrite();
    } else if (
      canClearPendingAfterError(
        response,
        responsePayload,
        retryingPending,
      )
    ) {
      try {
        await acknowledgePendingWrite(entry);
      } catch (error) {
        setWriteStatus(
          `Lokaler Recovery-ACK fehlgeschlagen: ${error.message}. Der Request bleibt vorsorglich gebunden.`,
          "warning",
        );
        return {response, payload: responsePayload};
      }
      pendingWrites.delete(scope);
    }
    return {response, payload: responsePayload};
  } catch (error) {
    setWriteStatus(
      `Transportstatus unbekannt: ${error.message} Derselbe Idempotency-Key bleibt für eine manuelle Wiederholung gebunden; es erfolgt kein automatischer Retry.`,
      "warning",
    );
    return null;
  }
}

function isPythonWhitespace(character) {
  const codePoint = character.codePointAt(0);
  return (
    (codePoint >= 0x0009 && codePoint <= 0x000d)
    || (codePoint >= 0x001c && codePoint <= 0x0020)
    || codePoint === 0x0085
    || codePoint === 0x00a0
    || codePoint === 0x1680
    || (codePoint >= 0x2000 && codePoint <= 0x200a)
    || codePoint === 0x2028
    || codePoint === 0x2029
    || codePoint === 0x202f
    || codePoint === 0x205f
    || codePoint === 0x3000
  );
}

function pythonStrip(value) {
  const characters = Array.from(value);
  let start = 0;
  let end = characters.length;
  while (start < end && isPythonWhitespace(characters[start])) start += 1;
  while (end > start && isPythonWhitespace(characters[end - 1])) end -= 1;
  return characters.slice(start, end).join("");
}

function requireUnicodeScalarText(value, fieldName) {
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint >= 0xd800 && codePoint <= 0xdfff) {
      throw new Error(fieldName + " darf keine ungültigen Unicode-Surrogates enthalten.");
    }
  }
}

function createPayload() {
  const rawCategoryPath = byId("create-category").value.split(">");
  if (rawCategoryPath.length < 2 || rawCategoryPath.length > 6) {
    throw new Error("Kategoriepfad muss zwischen 2 und 6 Labels enthalten.");
  }
  const categoryPath = rawCategoryPath.map((label) => {
    const normalized = pythonStrip(label);
    requireUnicodeScalarText(normalized, "Kategorie");
    if (!normalized) {
      throw new Error("Kategorie-Labels dürfen nicht leer sein.");
    }
    if (Array.from(normalized).length > 120) {
      throw new Error("Kategorie-Labels dürfen höchstens 120 Zeichen enthalten.");
    }
    return normalized;
  });
  if (new Set(categoryPath).size !== categoryPath.length) {
    throw new Error("Kategoriepfad darf keine doppelten Labels enthalten.");
  }

  const title = byId("create-title").value;
  requireUnicodeScalarText(title, "Titel");
  if (!pythonStrip(title)) {
    throw new Error("Titel darf nicht leer sein.");
  }
  if (title !== pythonStrip(title)) {
    throw new Error("Titel darf keine umgebenden Whitespaces enthalten.");
  }
  if (title.includes("\\r") || title.includes("\\n")) {
    throw new Error("Titel darf keine Zeilenumbrüche enthalten.");
  }
  if (title.length > 65) {
    throw new Error("Titel darf höchstens 65 UTF-16-Code-Units enthalten.");
  }

  const description = byId("create-description").value
    .replace(/\\r\\n/g, "\\n")
    .replace(/\\r/g, "\\n");
  requireUnicodeScalarText(description, "Beschreibung");
  if (!pythonStrip(description)) {
    throw new Error("Beschreibung darf nicht leer sein.");
  }
  if (description.length > 4000) {
    throw new Error("Beschreibung darf höchstens 4000 UTF-16-Code-Units enthalten.");
  }

  const price = Number(byId("create-price").value);
  if (!Number.isSafeInteger(price) || price < 1 || price > 99999999) {
    throw new Error("Festpreis muss eine ganze Euro-Zahl zwischen 1 und 99999999 sein.");
  }
  return {
    category_path: categoryPath,
    title,
    description,
    price_eur: price,
  };
}

function mediaContentType(file) {
  if (["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    return file.type;
  }
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".webp")) return "image/webp";
  return null;
}

function mediaStageFilename(file) {
  const contentType = mediaContentType(file);
  if (contentType === "image/jpeg") return "upload.jpg";
  if (contentType === "image/png") return "upload.png";
  if (contentType === "image/webp") return "upload.webp";
  throw new Error(`Nicht unterstützter Bildtyp: ${file.name}`);
}

async function discardStagedMedia(refs) {
  if (refs.length === 0) return;
  const {response, payload} = await proxyRequest(
    "POST",
    "/api/write/media/discard",
    {
      body: JSON.stringify({media_refs: refs}),
      contentType: "application/json",
    },
  );
  if (!response.ok || payload.discarded !== refs.length) {
    throw new Error(payload.error ?? `Media-Cleanup HTTP ${response.status}`);
  }
}

async function stageSelectedMedia(files) {
  if (files.length > 32) {
    throw new Error("Höchstens 32 Bilder können pro Create gestaged werden.");
  }
  let totalBytes = 0;
  for (const file of files) {
    const contentType = mediaContentType(file);
    if (contentType === null) {
      throw new Error(`Nicht unterstützter Bildtyp: ${file.name}`);
    }
    if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > 25 * 1024 * 1024) {
      throw new Error(`Ungültige Bildgröße: ${file.name}`);
    }
    totalBytes += file.size;
    if (totalBytes > 100 * 1024 * 1024) {
      throw new Error("Die ausgewählten Bilder überschreiten zusammen 100 MiB.");
    }
  }

  const refs = [];
  try {
    for (const file of files) {
      const {response, payload} = await proxyRequest(
        "POST",
        "/api/write/media/stage",
        {
          body: file,
          contentType: mediaContentType(file),
          filename: mediaStageFilename(file),
        },
      );
      if (!response.ok || typeof payload.media_ref !== "string") {
        throw new Error(payload.error ?? `Media-Staging HTTP ${response.status}`);
      }
      refs.push(payload.media_ref);
    }
    return refs;
  } catch (error) {
    if (refs.length > 0) {
      try {
        await discardStagedMedia(refs);
      } catch (cleanupError) {
        throw new Error(
          `${error.message}; lokale Media-Cleanup fehlgeschlagen: ${cleanupError.message}. Launcher neu starten, bevor erneut Bilder gestaged werden.`,
        );
      }
    }
    throw error;
  }
}

async function submitCreate(event) {
  event.preventDefault();
  if (!writeUiReady()) return;

  if (pendingWrites.has("create-media")) {
    await runPlatformWrite("create-media", "", "", null);
    return;
  }
  if (pendingWrites.has("create")) {
    await runPlatformWrite("create", "", "", null);
    return;
  }

  let payload;
  try {
    payload = createPayload();
  } catch (error) {
    setWriteStatus(error.message, "error");
    return;
  }

  const files = Array.from(byId("create-media").files ?? []);
  if (files.length === 0) {
    await runPlatformWrite("create", "POST", "/api/write/ads", payload);
    return;
  }

  if (createMediaInFlight) {
    setWriteStatus("Media-Create läuft bereits. Es wird kein zweiter Batch gestaged.", "warning");
    return;
  }
  createMediaInFlight = true;
  setWriteStatus("Bilder werden ausschließlich lokal gestaged …", "warning");
  try {
    const mediaRefs = await stageSelectedMedia(files);
    const result = await runPlatformWrite(
      "create-media",
      "POST",
      "/api/write/media/ads",
      {...payload, media_refs: mediaRefs},
    );
    if (
      result !== null
      && result.payload?.error === "dashboard_pending_write_conflict"
    ) {
      try {
        await discardStagedMedia(mediaRefs);
      } catch (cleanupError) {
        setWriteStatus(
          `Konkurrierender Create wurde nicht weitergeleitet, aber lokale Media-Cleanup fehlgeschlagen: ${cleanupError.message}.`,
          "error",
        );
        return;
      }
      setWriteStatus(
        "Konkurrierender Create wurde nicht weitergeleitet; dessen lokal gestagte Bilder wurden verworfen. Die bereits gebundene Anfrage bleibt maßgeblich.",
        "warning",
      );
    }
  } catch (error) {
    setWriteStatus(
      `Media-Staging fehlgeschlagen: ${error.message}. Es wurde kein neuer Plattform-Create gestartet.`,
      "error",
    );
  } finally {
    createMediaInFlight = false;
  }
}

function selectedAdId() {
  const adId = byId("manage-ad-id").value;
  if (!adId || !latestAdsById.has(adId)) {
    setWriteStatus("Zuerst eine aktuelle Anzeige über „Verwalten“ auswählen.", "error");
    return null;
  }
  const ad = latestAdsById.get(adId);
  if (ad.present === false && pendingForAd(adId) === null) {
    setWriteStatus(
      "Diese Anzeige ist nicht mehr vorhanden. Ohne gebundene Recovery-Anfrage sind keine neuen Writes zulässig.",
      "warning",
    );
    return null;
  }
  return adId;
}

async function runAdAction(action, method, suffix, payload = null) {
  const adId = selectedAdId();
  if (adId === null) return;
  const existing = pendingForAd(adId);
  const scope = `ad:${adId}:${action}`;
  if (existing !== null && existing.scope !== scope) {
    setWriteStatus(
      `Für diese Anzeige ist bereits „${existing.scope}“ mit unbekanntem/ausstehendem Ergebnis gebunden. Zuerst genau diese Anfrage erneut abfragen.`,
      "warning",
    );
    return;
  }
  return await runPlatformWrite(
    scope,
    method,
    `/api/write/ads/${encodeURIComponent(adId)}${suffix}`,
    payload,
    {adId},
  );
}

async function saveManagedAd() {
  const adId = selectedAdId();
  if (adId === null) return;

  const existing = pendingForAd(adId);
  if (existing !== null) {
    const result = await runAdAction("update", "PATCH", "", {});
    if (confirmedWriteResult(result) && pendingForAd(adId) === null) {
      rebaseManagedBaseline(adId, existing.payload);
    }
    return;
  }

  let payload;
  try {
    payload = managedContentChanges(adId);
  } catch (error) {
    setWriteStatus(error.message, "error");
    return;
  }
  if (Object.keys(payload).length === 0) {
    setWriteStatus("Keine Content-Änderungen zum Speichern.", "warning");
    return;
  }
  const result = await runAdAction("update", "PATCH", "", payload);
  if (confirmedWriteResult(result) && pendingForAd(adId) === null) {
    rebaseManagedBaseline(adId, payload);
  }
}

async function deleteManagedAd() {
  const adId = selectedAdId();
  if (adId === null) return;
  if (!window.confirm(`Anzeige ${adId} wirklich löschen?`)) return;
  await runAdAction("delete", "DELETE", "");
}

async function loadAnalytics() {
  const generation = ++analyticsRequestGeneration;
  const metric = byId("metric-select").value;
  const dimension = byId("dimension-select").value;
  if (!metric || !dimension) {
    renderGroups([]);
    renderRanking([]);
    return;
  }

  const [groups, ranking] = await Promise.all([
    getJson(`/api/analytics/groups?dimension=${encodeURIComponent(dimension)}&metric=${encodeURIComponent(metric)}`),
    getJson(`/api/analytics/ads?metric=${encodeURIComponent(metric)}`),
  ]);
  if (generation !== analyticsRequestGeneration) return;
  renderGroups(groups);
  renderRanking(ranking);
}

async function load() {
  const status = byId("status");
  status.textContent = "Lade lokale Daten …";
  try {
    const [summary, ads, metricsPayload, dimensionsPayload, contract, dashboardConfig] = await Promise.all([
      getJson("/api/summary"),
      getJson("/api/ads"),
      getJson("/api/analytics/metrics"),
      getJson("/api/analytics/dimensions"),
      getJson("/api/analytics/contract"),
      getJson("/api/dashboard/config"),
    ]);
    byId("tracked").textContent = summary.tracked_ads;
    byId("current").textContent = summary.current_ads;
    byId("absent").textContent = summary.absent_ads;
    byId("views").textContent =
      `${summary.views_total_known} / ${summary.views_observed_ads} Ads`;
    byId("watches").textContent =
      `${summary.watch_total_known} / ${summary.watch_observed_ads} Ads`;
    byId("replies").textContent =
      `${summary.replies_total_known} / ${summary.replies_observed_ads} Ads`;
    writeUiAvailable = dashboardConfig.write_ui_available === true;
    await refreshPendingWrites();
    renderWriteAvailability();
    renderAds(ads);
    renderAnalyticsContract(contract);

    const metricSelect = byId("metric-select");
    const dimensionSelect = byId("dimension-select");
    const previousMetric = metricSelect.value;
    const previousDimension = dimensionSelect.value;
    setOptions(metricSelect, metricsPayload.metrics, "Metrik auswählen …");
    setOptions(dimensionSelect, dimensionsPayload.dimensions);
    if (metricsPayload.metrics.includes(previousMetric)) {
      metricSelect.value = previousMetric;
    } else if (
      contract.objective_metric !== null
      && metricsPayload.metrics.includes(contract.objective_metric)
    ) {
      metricSelect.value = contract.objective_metric;
    }
    if (dimensionsPayload.dimensions.includes(previousDimension)) dimensionSelect.value = previousDimension;

    await loadAnalytics();
    status.textContent = "";
  } catch (error) {
    status.textContent = `Fehler beim Laden: ${error.message}`;
  }
}

byId("reload").addEventListener("click", load);
byId("metric-select").addEventListener("change", loadAnalytics);
byId("dimension-select").addEventListener("change", loadAnalytics);
byId("create-form").addEventListener("submit", submitCreate);
byId("manage-save").addEventListener("click", saveManagedAd);
byId("manage-pause").addEventListener(
  "click",
  () => runAdAction("pause", "POST", "/pause"),
);
byId("manage-activate").addEventListener(
  "click",
  () => runAdAction("activate", "POST", "/activate"),
);
byId("manage-delete").addEventListener("click", deleteManagedAd);
consumeWriteToken();
load();
"""


def _valid_ad_id(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 32
        and value.isascii()
        and value.isdigit()
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _single_query_value(query_string: str, name: str) -> str | None:
    values = parse_qs(query_string, keep_blank_values=True).get(name)
    if values is None or len(values) != 1 or not values[0]:
        return None
    return values[0]


def _dashboard_write_route(path: str, method: str) -> str | None:
    target = urlsplit(path)
    if target.query:
        return None
    parts = [unquote(item) for item in target.path.split("/") if item]
    if parts == ["api", "write", "ads"] and method == "POST":
        return "json"
    if parts == ["api", "write", "media", "stage"] and method == "POST":
        return "media"
    if parts == ["api", "write", "media", "discard"] and method == "POST":
        return "local_json"
    if parts == ["api", "write", "media", "ads"] and method == "POST":
        return "json"
    if len(parts) == 4 and parts[:3] == ["api", "write", "ads"]:
        if not _valid_ad_id(parts[3]):
            return None
        if method == "PATCH":
            return "json"
        if method == "DELETE":
            return "empty"
    if (
        len(parts) == 5
        and parts[:3] == ["api", "write", "ads"]
        and _valid_ad_id(parts[3])
        and parts[4] in {"pause", "activate"}
        and method == "POST"
    ):
        return "empty"
    return None


def _dashboard_pending_identity(
    path: str,
    method: str,
) -> tuple[str, str, str | None] | None:
    target = urlsplit(path)
    if target.query:
        return None
    parts = [unquote(item) for item in target.path.split("/") if item]
    if parts == ["api", "write", "ads"] and method == "POST":
        return ("create", "create", None)
    if parts == ["api", "write", "media", "ads"] and method == "POST":
        return ("create-media", "create", None)
    if len(parts) == 4 and parts[:3] == ["api", "write", "ads"]:
        ad_id = parts[3]
        if not _valid_ad_id(ad_id):
            return None
        if method == "PATCH":
            return (f"ad:{ad_id}:update", f"ad:{ad_id}", ad_id)
        if method == "DELETE":
            return (f"ad:{ad_id}:delete", f"ad:{ad_id}", ad_id)
    if (
        len(parts) == 5
        and parts[:3] == ["api", "write", "ads"]
        and _valid_ad_id(parts[3])
        and parts[4] in {"pause", "activate"}
        and method == "POST"
    ):
        ad_id = parts[3]
        return (
            f"ad:{ad_id}:{parts[4]}",
            f"ad:{ad_id}",
            ad_id,
        )
    return None


def _handler_factory(
    store: SnapshotStore,
    query: MarkQueryService,
    analytics: AnalyticsService,
    write_proxy: DashboardWriteProxy | None,
):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "mark-api-readonly/0.1"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            allow: str | None = None,
            idempotency_replayed: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; "
                "script-src 'self'; style-src 'self'; img-src 'none'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            if allow is not None:
                self.send_header("Allow", allow)
            if idempotency_replayed:
                self.send_header("Idempotency-Replayed", "true")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, value: object) -> None:
            self._send(
                status,
                _json_bytes(value),
                "application/json; charset=utf-8",
            )

        def _method_not_allowed(self) -> None:
            body = _json_bytes({"error": "method_not_allowed"})
            self._send(
                405,
                body,
                "application/json; charset=utf-8",
                allow="GET",
            )

        def _invalid_metric(self) -> None:
            self._send_json(
                400,
                {
                    "error": "invalid_metric",
                    "allowed_metrics": list(ANALYTICS_METRICS),
                },
            )

        def _invalid_dimension(self) -> None:
            self._send_json(
                400,
                {
                    "error": "invalid_dimension",
                    "allowed_dimensions": list(ANALYTICS_DIMENSIONS),
                },
            )

        def _write_token_authorized(self, *, require_origin: bool) -> bool:
            if write_proxy is None:
                return False
            marker = self.headers.get_all(_DASHBOARD_WRITE_MARKER_HEADER) or []
            tokens = self.headers.get_all(_DASHBOARD_WRITE_TOKEN_HEADER) or []
            if (
                len(marker) != 1
                or marker[0] != _DASHBOARD_WRITE_MARKER_VALUE
                or len(tokens) != 1
                or not hmac.compare_digest(tokens[0], write_proxy.ui_token)
            ):
                return False
            if self.headers.get("Authorization") is not None:
                return False
            if require_origin:
                origins = self.headers.get_all("Origin") or []
                server_host, server_port = self.server.server_address
                expected_origin = _dashboard_http_origin(
                    str(server_host), int(server_port)
                )
                if (
                    len(origins) != 1
                    or not hmac.compare_digest(origins[0], expected_origin)
                ):
                    return False
            fetch_sites = self.headers.get_all("Sec-Fetch-Site") or []
            if fetch_sites and (
                len(fetch_sites) != 1 or fetch_sites[0] != "same-origin"
            ):
                return False
            return True

        def _write_session_authorized(self) -> bool:
            return self._write_token_authorized(require_origin=True)

        def _content_length(self, *, max_bytes: int) -> int:
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("transfer_encoding_not_allowed")
            values = self.headers.get_all("Content-Length") or []
            if not values:
                return 0
            if len(values) != 1:
                raise ValueError("invalid_content_length")
            try:
                value = int(values[0], 10)
            except ValueError as exc:
                raise ValueError("invalid_content_length") from exc
            if value < 0 or value > max_bytes:
                raise ValueError("invalid_content_length")
            return value

        def _read_proxy_body(self, body_kind: str) -> tuple[bytes, dict[str, str]]:
            headers: dict[str, str] = {}
            if body_kind == "empty":
                length = self._content_length(max_bytes=0)
                if length != 0:
                    raise ValueError("request_body_not_allowed")
                return b"", headers

            if body_kind in {"json", "local_json"}:
                content_type = self.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("content_type_must_be_json")
                length = self._content_length(max_bytes=_MAX_PROXY_JSON_BODY_BYTES)
                if length == 0:
                    raise ValueError("json_body_required")
                headers["Content-Type"] = "application/json"
            elif body_kind == "media":
                content_type = (
                    self.headers.get("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise ValueError("unsupported_media_type")
                filenames = self.headers.get_all("X-Mark-Media-Filename") or []
                if len(filenames) != 1:
                    raise ValueError("invalid_media_filename")
                length = self._content_length(max_bytes=_MAX_PROXY_MEDIA_BODY_BYTES)
                if length == 0:
                    raise ValueError("media_body_required")
                headers["Content-Type"] = content_type
                headers["X-Mark-Media-Filename"] = filenames[0]
            else:
                raise ValueError("unsupported_proxy_body")

            previous_timeout = self.connection.gettimeout()
            self.connection.settimeout(_PROXY_BODY_READ_TIMEOUT_SECONDS)
            try:
                try:
                    body = self.rfile.read(length)
                except TimeoutError as exc:
                    raise ValueError("request_body_timeout") from exc
            finally:
                try:
                    self.connection.settimeout(previous_timeout)
                except OSError:
                    pass
            if len(body) != length:
                raise ValueError("incomplete_request_body")
            return body, headers

        def _proxy_write(self, method: str) -> None:
            if write_proxy is None:
                self._method_not_allowed()
                return
            body_kind = _dashboard_write_route(self.path, method)
            if body_kind is None:
                self._send_json(404, {"error": "not_found"})
                return
            if not self._write_session_authorized():
                self._send_json(
                    403,
                    {
                        "error": "write_session_required",
                        "platform_retry_authorized": False,
                    },
                )
                return

            if urlsplit(self.path).path == "/api/write/media/stage":
                try:
                    create_recovery_pending = any(
                        record.resource_key == "create"
                        for record in store.dashboard_pending_writes()
                    )
                except Exception:
                    self._send_json(
                        500,
                        {
                            "error": "dashboard_pending_store_error",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
                if create_recovery_pending:
                    self._send_json(
                        409,
                        {
                            "error": "dashboard_pending_write_conflict",
                            "platform_retry_authorized": False,
                        },
                    )
                    return

            headers: dict[str, str] = {
                "Authorization": f"Bearer {write_proxy.bearer_token}",
            }
            platform_write = body_kind in {"json", "empty"}
            idempotency_key: str | None = None
            if platform_write:
                values = self.headers.get_all("Idempotency-Key") or []
                if (
                    len(values) != 1
                    or _DASHBOARD_IDEMPOTENCY_KEY_RE.fullmatch(values[0]) is None
                ):
                    self._send_json(
                        400,
                        {
                            "error": "invalid_or_missing_idempotency_key",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
                idempotency_key = values[0]
                headers["Idempotency-Key"] = idempotency_key
            elif self.headers.get("Idempotency-Key") is not None:
                self._send_json(
                    400,
                    {
                        "error": "idempotency_key_not_allowed_for_local_media_operation",
                        "platform_retry_authorized": False,
                    },
                )
                return

            try:
                body, body_headers = self._read_proxy_body(body_kind)
            except ValueError as exc:
                self._send_json(
                    400,
                    {
                        "error": str(exc),
                        "platform_retry_authorized": False,
                    },
                )
                return
            headers.update(body_headers)
            headers["Content-Length"] = str(len(body))

            pending_scope: str | None = None
            if platform_write:
                assert idempotency_key is not None
                identity = _dashboard_pending_identity(self.path, method)
                if identity is None:
                    self._send_json(
                        500,
                        {
                            "error": "dashboard_pending_identity_error",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
                pending_scope, resource_key, pending_ad_id = identity
                pending_payload_json: str | None = None
                if body_kind == "json":
                    try:
                        pending_payload = json.loads(body)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._send_json(
                            400,
                            {
                                "error": "invalid_json",
                                "platform_retry_authorized": False,
                            },
                        )
                        return
                    if not isinstance(pending_payload, dict):
                        self._send_json(
                            400,
                            {
                                "error": "json_body_must_be_object",
                                "platform_retry_authorized": False,
                            },
                        )
                        return
                    pending_payload_json = _json_bytes(
                        pending_payload
                    ).decode("utf-8")
                try:
                    store.claim_dashboard_pending_write(
                        scope=pending_scope,
                        resource_key=resource_key,
                        idempotency_key=idempotency_key,
                        method=method,
                        path=urlsplit(self.path).path,
                        payload_json=pending_payload_json,
                        ad_id=pending_ad_id,
                    )
                except ValueError:
                    self._send_json(
                        409,
                        {
                            "error": "dashboard_pending_write_conflict",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
                except Exception:
                    self._send_json(
                        500,
                        {
                            "error": "dashboard_pending_store_error",
                            "platform_retry_authorized": False,
                        },
                    )
                    return

            connection = http.client.HTTPConnection(
                write_proxy.host,
                write_proxy.port,
                timeout=float(write_proxy.timeout_seconds),
            )
            try:
                try:
                    connection.request(
                        method,
                        urlsplit(self.path).path,
                        body=body,
                        headers=headers,
                    )
                    response = connection.getresponse()
                    response_status = response.status
                    response_body = response.read(
                        _MAX_PROXY_RESPONSE_BYTES + 1
                    )
                    if len(response_body) > _MAX_PROXY_RESPONSE_BYTES:
                        raise ValueError("write_proxy_response_too_large")
                    content_type = response.getheader("Content-Type", "")
                    if not content_type.lower().startswith("application/json"):
                        raise ValueError("write_proxy_response_not_json")
                    try:
                        response_payload = json.loads(response_body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(
                            "write_proxy_response_not_json"
                        ) from exc
                    if not isinstance(response_payload, dict):
                        raise ValueError("write_proxy_response_not_json")
                    replayed = (
                        response.getheader("Idempotency-Replayed") == "true"
                    )
                except Exception:
                    self._send_json(
                        502,
                        {
                            "error": "write_proxy_transport_unknown",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
            finally:
                connection.close()

            # The backend operation and its response are settled at this point.
            # A later dashboard-client disconnect must not be misreported as an
            # unknown backend transport outcome or trigger another response.
            self._send(
                response_status,
                response_body,
                "application/json; charset=utf-8",
                idempotency_replayed=replayed,
            )

        def do_GET(self) -> None:
            target = urlsplit(self.path)
            path = target.path

            if path == "/":
                self._send(
                    200,
                    _DASHBOARD_HTML.encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path == "/dashboard.css":
                self._send(
                    200,
                    _DASHBOARD_CSS.encode("utf-8"),
                    "text/css; charset=utf-8",
                )
                return
            if path == "/dashboard.js":
                self._send(
                    200,
                    _DASHBOARD_JS.encode("utf-8"),
                    "text/javascript; charset=utf-8",
                )
                return
            if path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            if path == "/api/dashboard/config":
                self._send_json(
                    200,
                    {"write_ui_available": write_proxy is not None},
                )
                return
            if path == "/api/dashboard/pending-writes":
                if write_proxy is None:
                    self._send_json(404, {"error": "not_found"})
                    return
                if not self._write_token_authorized(require_origin=False):
                    self._send_json(
                        403,
                        {
                            "error": "write_session_required",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
                try:
                    records = store.dashboard_pending_writes()
                    pending_rows = [
                        {
                            "scope": record.scope,
                            "key": record.idempotency_key,
                            "method": record.method,
                            "path": record.path,
                            "payload": (
                                json.loads(record.payload_json)
                                if record.payload_json is not None
                                else None
                            ),
                            "adId": record.ad_id,
                            "acknowledged": record.acknowledged,
                        }
                        for record in records
                    ]
                except Exception:
                    self._send_json(
                        500,
                        {
                            "error": "dashboard_pending_store_error",
                            "platform_retry_authorized": False,
                        },
                    )
                    return
                self._send_json(
                    200,
                    {"pending_writes": pending_rows},
                )
                return
            if path == "/api/summary":
                self._send_json(200, summary_to_dict(query.summary()))
                return
            if path == "/api/ads":
                self._send_json(
                    200,
                    [ad_view_to_dict(item) for item in query.latest_ads()],
                )
                return
            if path == "/api/email-reactions":
                self._send_json(
                    200,
                    [
                        email_reaction_view_to_dict(item)
                        for item in query.email_reactions()
                    ],
                )
                return
            if path == "/api/analytics/metrics":
                self._send_json(200, {"metrics": list(ANALYTICS_METRICS)})
                return
            if path == "/api/analytics/contract":
                self._send_json(
                    200,
                    analytics_contract_to_dict(analytics.contract),
                )
                return
            if path == "/api/analytics/dimensions":
                self._send_json(
                    200,
                    {"dimensions": list(ANALYTICS_DIMENSIONS)},
                )
                return
            if path == "/api/analytics/ads":
                metric = _single_query_value(target.query, "metric")
                if metric not in ANALYTICS_METRICS:
                    self._invalid_metric()
                    return
                self._send_json(
                    200,
                    [
                        ad_metric_ranking_to_dict(item)
                        for item in analytics.rank_ads(metric)
                    ],
                )
                return
            if path == "/api/analytics/groups":
                dimension = _single_query_value(target.query, "dimension")
                if dimension not in ANALYTICS_DIMENSIONS:
                    self._invalid_dimension()
                    return
                metric = _single_query_value(target.query, "metric")
                if metric not in ANALYTICS_METRICS:
                    self._invalid_metric()
                    return
                self._send_json(
                    200,
                    [
                        group_metric_ranking_to_dict(item)
                        for item in analytics.group_rankings(dimension, metric)
                    ],
                )
                return

            parts = [unquote(item) for item in path.split("/") if item]
            if (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "ads"
                and parts[3] in {"history", "reactions", "email-reactions"}
            ):
                ad_id = parts[2]
                if not _valid_ad_id(ad_id):
                    self._send_json(400, {"error": "invalid_ad_id"})
                    return

                if parts[3] == "history":
                    history = query.ad_history(ad_id)
                    if not history:
                        self._send_json(404, {"error": "ad_not_found"})
                        return
                    self._send_json(
                        200,
                        [ad_snapshot_to_dict(item) for item in history],
                    )
                    return

                if parts[3] == "email-reactions":
                    email_reaction = query.email_reaction(ad_id)
                    if email_reaction is None:
                        self._send_json(404, {"error": "ad_not_found"})
                        return
                    self._send_json(
                        200,
                        email_reaction_view_to_dict(email_reaction),
                    )
                    return

                reactions = query.reaction_history(ad_id)
                if not reactions:
                    self._send_json(404, {"error": "ad_not_found"})
                    return
                self._send_json(
                    200,
                    [
                        reaction_snapshot_to_dict(item)
                        for item in reactions
                    ],
                )
                return

            self._send_json(404, {"error": "not_found"})

        def do_HEAD(self) -> None:
            self._method_not_allowed()

        def _ack_dashboard_pending_write(self) -> None:
            target = urlsplit(self.path)
            if target.path != "/api/dashboard/pending-writes/ack" or target.query:
                self._send_json(404, {"error": "not_found"})
                return
            if write_proxy is None:
                self._send_json(404, {"error": "not_found"})
                return
            if not self._write_session_authorized():
                self._send_json(
                    403,
                    {
                        "error": "write_session_required",
                        "platform_retry_authorized": False,
                    },
                )
                return
            if self.headers.get("Idempotency-Key") is not None:
                self._send_json(
                    400,
                    {
                        "error": "idempotency_key_not_allowed_for_pending_ack",
                        "platform_retry_authorized": False,
                    },
                )
                return
            try:
                body, _ = self._read_proxy_body("local_json")
                payload = json.loads(body)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"scope", "idempotency_key"}
                    or not isinstance(payload["scope"], str)
                    or not isinstance(payload["idempotency_key"], str)
                ):
                    raise ValueError("invalid_pending_ack")
                phase = store.acknowledge_dashboard_pending_write(
                    scope=payload["scope"],
                    idempotency_key=payload["idempotency_key"],
                )
                if phase == "missing":
                    raise ValueError("dashboard pending write is missing")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_json(
                    409,
                    {
                        "error": "dashboard_pending_write_conflict",
                        "platform_retry_authorized": False,
                    },
                )
                return
            except Exception:
                self._send_json(
                    500,
                    {
                        "error": "dashboard_pending_store_error",
                        "platform_retry_authorized": False,
                    },
                )
                return
            self._send_json(
                200,
                {
                    "acknowledged": True,
                    "finalized": phase == "finalized",
                },
            )

        def do_POST(self) -> None:
            if urlsplit(self.path).path == "/api/dashboard/pending-writes/ack":
                self._ack_dashboard_pending_write()
                return
            self._proxy_write("POST")

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._proxy_write("PATCH")

        def do_DELETE(self) -> None:
            self._proxy_write("DELETE")

        def do_OPTIONS(self) -> None:
            # Deliberately no CORS/preflight surface. Browser writes must be
            # same-origin and carry the per-process dashboard write token.
            self._method_not_allowed()

    return DashboardHandler


class LoopbackDashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(
    store: SnapshotStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    analytics_contract: AnalyticsContract | None = None,
    write_proxy: DashboardWriteProxy | None = None,
) -> LoopbackDashboardServer:
    if host != "127.0.0.1":
        raise ValueError("dashboard must bind to 127.0.0.1")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 0 <= port <= 65535
    ):
        raise ValueError("port must be an integer between 0 and 65535")

    query = MarkQueryService(store)
    analytics = AnalyticsService(store, contract=analytics_contract)
    return LoopbackDashboardServer(
        (host, port),
        _handler_factory(store, query, analytics, write_proxy),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the local read-only mark-api dashboard.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Path to the mark-api SQLite database.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Loopback port (default: 8765).",
    )
    parser.add_argument(
        "--reaction-metric",
        choices=REACTION_METRICS,
        default=None,
        help=(
            "Explicit interpretation of 'wie viele geschrieben haben'; "
            "unset by default."
        ),
    )
    parser.add_argument(
        "--objective-metric",
        choices=ANALYTICS_METRICS,
        default=None,
        help=(
            "Explicit analytics objective used to preselect rankings; "
            "unset by default."
        ),
    )
    args = parser.parse_args(argv)

    store = SnapshotStore(args.db)
    contract = AnalyticsContract(
        reaction_metric=args.reaction_metric,
        objective_metric=args.objective_metric,
    )
    server = create_server(
        store,
        port=args.port,
        analytics_contract=contract,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())