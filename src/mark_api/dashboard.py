from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .query import (
    MarkQueryService,
    ad_snapshot_to_dict,
    ad_view_to_dict,
    reaction_snapshot_to_dict,
    summary_to_dict,
)
from .storage import SnapshotStore


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
        <p>Read-only Sicht auf die lokale SQLite-Historie.</p>
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
h1, h2, p { margin-top: 0; }
header p { color: #aeb5c0; margin-bottom: 0; }
button {
  border: 1px solid #424a57;
  background: #20252d;
  color: inherit;
  border-radius: 8px;
  padding: 9px 14px;
  cursor: pointer;
}
button:hover { background: #2a313b; }
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
.panel { padding: 18px; }
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
@media (max-width: 640px) {
  header { align-items: flex-start; flex-direction: column; }
}
"""

_DASHBOARD_JS = """
const byId = (id) => document.getElementById(id);

const display = (value) => value === null || value === undefined ? "—" : String(value);

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

function renderAds(ads) {
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
    body.append(row);
  }
}

async function load() {
  const status = byId("status");
  status.textContent = "Lade lokale Daten …";
  try {
    const [summary, ads] = await Promise.all([
      getJson("/api/summary"),
      getJson("/api/ads"),
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
    renderAds(ads);
    status.textContent = "";
  } catch (error) {
    status.textContent = `Fehler beim Laden: ${error.message}`;
  }
}

byId("reload").addEventListener("click", load);
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


def _handler_factory(query: MarkQueryService):
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

        def do_GET(self) -> None:
            path = urlsplit(self.path).path

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
            if path == "/api/summary":
                self._send_json(200, summary_to_dict(query.summary()))
                return
            if path == "/api/ads":
                self._send_json(
                    200,
                    [ad_view_to_dict(item) for item in query.latest_ads()],
                )
                return

            parts = [unquote(item) for item in path.split("/") if item]
            if (
                len(parts) == 4
                and parts[0] == "api"
                and parts[1] == "ads"
                and parts[3] in {"history", "reactions"}
            ):
                ad_id = parts[2]
                if not _valid_ad_id(ad_id):
                    self._send_json(400, {"error": "invalid_ad_id"})
                    return

                history = query.ad_history(ad_id)
                if not history:
                    self._send_json(404, {"error": "ad_not_found"})
                    return

                if parts[3] == "history":
                    self._send_json(
                        200,
                        [ad_snapshot_to_dict(item) for item in history],
                    )
                    return

                self._send_json(
                    200,
                    [
                        reaction_snapshot_to_dict(item)
                        for item in query.reaction_history(ad_id)
                    ],
                )
                return

            self._send_json(404, {"error": "not_found"})

        def do_HEAD(self) -> None:
            self._method_not_allowed()

        def do_POST(self) -> None:
            self._method_not_allowed()

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:
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
    return LoopbackDashboardServer(
        (host, port),
        _handler_factory(query),
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
    args = parser.parse_args(argv)

    store = SnapshotStore(args.db)
    server = create_server(store, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
