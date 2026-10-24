"""Synthetic end-to-end dashboard -> Write API -> SQLite CRUD lifecycle.

No browser, provider or Kleinanzeigen account: this exercises the actual local
HTTP proxy and persistence under a bounded fake in-memory owner service.
The installed-wheel acceptance is a separate, independent release gate.
"""
from __future__ import annotations

import json, tempfile, threading, unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError
from mark_api.application import MarkService
from mark_api.dashboard import DashboardWriteProxy, create_server
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.results import ReadResult
from mark_api.storage import SnapshotStore
from mark_api.write_api import WriteApiAccess, WriteCapability, create_write_api_server

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
BACKEND_TOKEN = "isolated-test-backend-operator-token-12345"
UI_TOKEN = "isolated-test-dashboard-operator-token-1234"

class SyntheticPlatform:
    def __init__(self):
        self.ads = {"314": AdSnapshot(ad_id="314", observed_at=NOW, source="synthetic-owner", lifecycle_state=LifecycleState.ACTIVE, title="Original")}
        self.effects = []
        self.backend_requests = []
    def read_ads(self):
        values = tuple(self.ads.values())
        return ReadResult.success_nonempty(values) if values else ReadResult.success_empty(())
    def update_content(self, ad_id, *, title=None, description=None):
        original = self.ads[ad_id]
        self.ads[ad_id] = replace(original, title=original.title if title is None else title,
           description=original.description if description is None else description,
           observed_at=NOW + timedelta(seconds=len(self.effects) + 1))
        self.effects.append("update_content")
    def set_state(self, ad_id, state):
        self.ads[ad_id] = replace(self.ads[ad_id], lifecycle_state=state,
             observed_at=NOW + timedelta(seconds=len(self.effects) + 1))
        self.effects.append("set_state:" + state.value)
    def create_ad(self, req):
        self.ads["315"] = AdSnapshot(ad_id="315", observed_at=NOW + timedelta(seconds=2),
             source="synthetic-owner", lifecycle_state=LifecycleState.ACTIVE,
             title=req.title, description=req.description)
        self.effects.append("create")
    def delete_ad(self, ad_id):
        del self.ads[ad_id]
        self.effects.append("delete")

class PerAdReader:
    def __init__(self, platform, ad_id):
        self.platform, self.ad_id = platform, ad_id
    def read_ads(self):
        found = tuple(v for v in self.platform.ads.values() if v.ad_id == self.ad_id)
        return ReadResult.success_nonempty(found) if found else ReadResult.success_empty(())

def service_for(platform, store):
    return MarkService(
        owner_reader=platform, management_reader=platform,
        delete_confirmation_reader=platform, reaction_reader=None,
        state_writer=platform, delete_writer=platform,
        content_writer=platform, create_writer=platform,
        content_reader_factory=lambda ad_id: PerAdReader(platform, ad_id),
        store=store, writes_enabled=True, clock=lambda: NOW + timedelta(seconds=10))

@contextmanager
def servers(platform, store):
    access = WriteApiAccess(principal="synthetic-lifecycle-only", bearer_token=BACKEND_TOKEN,
         capabilities=frozenset(WriteCapability) - {WriteCapability.CREATE_MEDIA}, writes_enabled=True)
    backend = create_write_api_server(service_for(platform, store), store, access, port=0)
    dispatch_write = backend.RequestHandlerClass._dispatch_write

    def counted_dispatch(handler, method):
        platform.backend_requests.append((method, handler.path))
        return dispatch_write(handler, method)

    backend.RequestHandlerClass._dispatch_write = counted_dispatch
    dashboard = create_server(store, port=0, write_proxy=DashboardWriteProxy(
        host="127.0.0.1", port=backend.server_port, bearer_token=BACKEND_TOKEN, ui_token=UI_TOKEN,
        backend_principal=access.principal, local_reconciliation_enabled=True))
    threads = []
    try:
        for server in (backend,dashboard):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        yield "http://127.0.0.1:" + str(dashboard.server_port)
    finally:
        for server in (dashboard,backend):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=4)
            if thread.is_alive():
                raise AssertionError("unjoined HTTP thread")

def request(base, path, *, method="GET", payload=None, key=None, authorized=True):
    headers = {"Accept": "application/json"}
    if method == "GET" and path == "/api/dashboard/pending-writes":
        headers.update({"X-Mark-Dashboard-Write":"1","X-Mark-Dashboard-Token":UI_TOKEN})
    if method != "GET":
        headers.update({"Origin":base,"X-Mark-Dashboard-Write":"1"})
        if key is not None:
            headers["Idempotency-Key"]=key
        if authorized:
            headers["X-Mark-Dashboard-Token"] = UI_TOKEN
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(base+path, method=method, data=data, headers=headers)
    op = build_opener(ProxyHandler({}))
    try:
        resp=op.open(req,timeout=3)
    except HTTPError as e:
        resp=e
    with resp:
        return resp.status,json.loads(resp.read()),dict(resp.headers)

class DashboardFullLifecycleE2ETests(unittest.TestCase):
    def test_create_edit_pause_activate_delete_restart_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=SnapshotStore(Path(tmp)/"mark.sqlite")
            platform=SyntheticPlatform()
            store.append_ad_snapshot(platform.ads["314"])
            with servers(platform,store) as base:
                data = {"category_path":["Freizeit","Sammeln"],"title":"Created local demo",
                        "description":"Only synthetic platform state","price_eur":10}
                steps=[
                 ("POST","/api/write/ads",data,"create-315","create"),
                 ("PATCH","/api/write/ads/315",{"title":"Changed local demo","description":"Synthetic edit"},"edit-315","update_content"),
                 ("POST","/api/write/ads/315/pause",None,"pause-315","set_state:paused"),
                 ("POST","/api/write/ads/315/activate",None,"activate-315","set_state:active"),
                 ("DELETE","/api/write/ads/315",None,"delete-315","delete")
                ]
                for method,path,payload,key,effect in steps:
                    before=len(platform.effects)
                    status,body,headers=request(base,path,method=method,payload=payload,key=key)
                    self.assertEqual(status,200,(method,path,body))
                    self.assertEqual(body["operation_receipt"]["outcome"],"confirmed",(path,body))
                    self.assertEqual(platform.effects[before:],[effect])
                    status2,body2,headers2=request(base,path,method=method,payload=payload,key=key)
                    self.assertEqual((status2,body2),(status,body))
                    self.assertEqual(headers2.get("Idempotency-Replayed"),"true")
                    self.assertEqual(len(platform.effects),before+1)
                    self.assertEqual(len(platform.backend_requests), before + 1)
                    scope = "create" if effect == "create" else "ad:315:" + (
                        "update" if effect == "update_content" else
                        ("pause" if effect == "set_state:paused" else
                         "activate" if effect == "set_state:active" else "delete")
                    )
                    # The real browser observes the completed response, then
                    # ACKs recovery ownership in two bounded durable phases.
                    for expected_finalized in (False,True):
                        ack_status,ack_body,_=request(base, "/api/dashboard/pending-writes/ack",
                            method="POST", key=None, payload={"scope":scope,"idempotency_key":key})
                        self.assertEqual(ack_status, 200, (scope,ack_body))
                        self.assertTrue(ack_body["acknowledged"])
                        self.assertIs(ack_body["finalized"], expected_finalized)
                    self.assertEqual(store.dashboard_pending_writes(), ())
                    pending_status, pending, _ = request(base,"/api/dashboard/pending-writes")
                    self.assertEqual(pending_status,200)
                    self.assertEqual(pending["pending_writes"], [])
                    if effect!="delete":
                        rows={r["ad_id"]:r for r in request(base,"/api/ads")[1]}
                        self.assertEqual(rows["315"]["title"],"Changed local demo" if effect!="create" else "Created local demo")
                self.assertNotIn("315",platform.ads)
                self.assertEqual(platform.effects,["create","update_content","set_state:paused","set_state:active","delete"])
                self.assertGreaterEqual(len(store.ad_history("315")),4)
            with servers(platform,store) as base:
                status,body,headers=request(base,"/api/write/ads/315",method="DELETE",key="delete-315")
                self.assertEqual(status,200,body)
                self.assertEqual(headers.get("Idempotency-Replayed"),"true")
                self.assertEqual(len(platform.effects),5)
                self.assertEqual(len(platform.backend_requests), 5)


    def test_restart_fences_unacknowledged_write_without_external_retry(self):
        """Unknown client acknowledgment never authorizes a second platform action."""
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "mark.sqlite")
            platform = SyntheticPlatform()
            store.append_ad_snapshot(platform.ads["314"])
            payload = {
                "category_path": ["Freizeit", "Sammeln"],
                "title": "Synthetic saved listing",
                "description": "Local test only",
                "price_eur": 10,
            }
            with servers(platform, store) as base:
                status, receipt, _ = request(
                    base, "/api/write/ads", method="POST",
                    payload=payload, key="first-create-key",
                )
                self.assertEqual(status, 200, receipt)
                self.assertEqual(receipt["operation_receipt"]["outcome"], "confirmed")
                self.assertEqual(platform.effects, ["create"])
                pending = request(base, "/api/dashboard/pending-writes")[1]["pending_writes"]
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["scope"], "create")
                self.assertFalse(pending[0]["acknowledged"])
                # Stop both HTTP servers without the browser recovery ACK.
            with servers(platform, store) as base:
                status, denied, _ = request(
                    base, "/api/write/ads", method="POST",
                    payload=payload, key="new-create-key",
                )
                self.assertEqual(status, 409, denied)
                self.assertEqual(denied["error"], "dashboard_pending_write_conflict")
                self.assertEqual(platform.effects, ["create"])
                status, replay, headers = request(
                    base, "/api/write/ads", method="POST",
                    payload=payload, key="first-create-key",
                )
                self.assertEqual((status, replay), (200, receipt))
                self.assertEqual(headers.get("Idempotency-Replayed"), "true")
                self.assertEqual(platform.effects, ["create"])
                ack = {"scope": "create", "idempotency_key": "first-create-key"}
                for finalized in (False, True):
                    code, result, _ = request(
                        base, "/api/dashboard/pending-writes/ack",
                        method="POST", payload=ack,
                    )
                    self.assertEqual((code, result), (
                        200, {"acknowledged": True, "finalized": finalized}
                    ))
                self.assertEqual(
                    request(base, "/api/dashboard/pending-writes")[1]["pending_writes"], []
                )
                status, edit, _ = request(
                    base, "/api/write/ads/315", method="PATCH",
                    payload={"title": "Post-recovery synthetic edit"},
                    key="after-recovery-edit",
                )
                self.assertEqual(status, 200, edit)
                self.assertEqual(edit["operation_receipt"]["outcome"], "confirmed")
                self.assertEqual(platform.effects, ["create", "update_content"])
                self.assertEqual(len(platform.backend_requests), 2)


if __name__ == "__main__":
    unittest.main()
