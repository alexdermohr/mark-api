"""Opt-in REAL Linux/Docker SQLite-owner recovery-fence security exercise.

The untrusted executor UID 50044 has no data mount in the intended topology.
This deliberately mounts a *read-only test-only* volume under UID 50044 to
prove that DAC independently denies old main/WAL/SHM raw access. No reused
installed Mark DB/volume, no Chrome, no platform Write and no host UID changes.

Opt-in Docker mutation fails closed until independent exclusive daemon
administration is installed. This is NOT historical FD/mmap revocation or a
production recovery release gate.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("MARK_STORAGE_OWNER_TEST_IMAGE", "")
IMAGE_OK = bool(re.fullmatch(r"sha256:[a-f0-9]{64}", IMAGE))
# Synthetic upstream Python images and the actual Mark release image install
# Python at distinct audited paths. Never silently select an arbitrary binary.
PYTHON = os.environ.get("MARK_STORAGE_OWNER_TEST_PYTHON", "/usr/local/bin/python")
if PYTHON not in {"/usr/local/bin/python", "/opt/mark-api/venv/bin/python"}:
    raise ValueError("unattested Docker test interpreter path")


if os.environ.get("MARK_REQUIRE_STORAGE_OWNER_UID") == "1" and not IMAGE_OK:
    raise ValueError("required Docker UID test lacks a pinned image")


_DOCKER_AUTHORITY_ERROR = (
    "exclusive Docker-admin authority not established: shared-daemon Docker "
    "resource mutation forbidden before subprocess"
)


def _require_exclusive_docker_admin_authority() -> None:
    """Real test may only run with independent, enforced daemon exclusivity."""
    if os.environ.get("MARK_REQUIRE_STORAGE_OWNER_UID") == "1":
        raise AssertionError(_DOCKER_AUTHORITY_ERROR)
    raise unittest.SkipTest(_DOCKER_AUTHORITY_ERROR)


def _docker(*args: str, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    # Positive allowlist: neither name-based cleanup nor command aliases may
    # dispatch without independently enforced exclusive Docker authority.
    if not (
        args[:1] == ("info",)
        or args[:2] in (
            ("image", "inspect"),
            ("container", "inspect"),
            ("volume", "inspect"),
        )
    ):
        raise AssertionError(_DOCKER_AUTHORITY_ERROR)
    value = subprocess.run(
        ["/usr/bin/docker", *args], capture_output=True, text=True,
        timeout=45, check=False,
    )
    if value.returncode and not allow_failure:
        raise AssertionError("isolated Docker action failed: " + value.stderr[-900:])
    return value


_SEED = r"""
import os
from pathlib import Path
from mark_api.storage import SnapshotStore
database=Path('/data')
rpc=Path('/rpc')
os.chown(database,50046,50046)
os.chmod(database,0o700)
os.chown(rpc,50046,50044)
os.chmod(rpc,0o2710)
os.setgroups([])
os.setgid(50046)
os.setuid(50046)
from datetime import datetime, timezone
from mark_api.domain import ReactionSnapshot
store=SnapshotStore(database/'mark.sqlite')
store.append_reaction_snapshot(ReactionSnapshot(
    ad_id='777', observed_at=datetime(2026,10,11,12,0,tzinfo=timezone.utc),
    source='kleinanzeigen:email-only', conversation_count=2,
    unique_buyer_count=3, inbound_message_count=5,
))
from mark_api.domain import InboundMessageEvent, AdClassification
store.append_inbound_message_events((
    InboundMessageEvent(
        ad_id='777', conversation_id='buyer1',
        provider_message_id='real-uid-event-1',
        observed_at=datetime(2026,10,11,12,0,tzinfo=timezone.utc),
        source='email-only-synthetic',
    ),
    InboundMessageEvent(
        ad_id='777', conversation_id='buyer2',
        provider_message_id='real-uid-event-2',
        observed_at=datetime(2026,10,11,12,1,tzinfo=timezone.utc),
        source='email-only-synthetic',
    ),
))
store.append_classification(AdClassification(
    ad_id='777', observed_at=datetime(2026,10,11,12,0,tzinfo=timezone.utc),
    source='manual', city='Berlin',
))
print('PRIVATE_EXISTING_SCHEMA_V2',flush=True)
"""

_SERVICE = r"""
from pathlib import Path
from mark_api.storage_owner import StorageOwnerServer
import time
server=StorageOwnerServer(
    database=Path('/data/mark.sqlite'),
    socket_path=Path('/rpc/owner.sock'),
    runtime_uid=50044,runtime_gid=50044,
)
server.start()
print('PRIVATE_STORAGE_OWNER_READY',flush=True)
while True:time.sleep(2)
"""

_RUNTIME = r"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from mark_api.storage_owner import StorageOwnerClient, StorageOwnerError
from mark_api.storage import WriteApiRequestRecord
from mark_api.domain import AdSnapshot, LifecycleState, ReactionSnapshot
from mark_api.results import ReadResult
from mark_api.storage_owner import _snapshot_as_json
import json, socket, time
client=StorageOwnerClient(Path('/rpc/owner.sock'),expected_owner_uid=50046)
assert client.ready()
assert client.sync_status()=={
    'state':'never_attempted',
    'latest_attempt':None,
    'last_successful_attempt':None,
}
assert client.sync_recovery_status()=={
    'unresolved_count':0,'oldest_unresolved_attempt':None,
}
for invalid in (
    '{"title":"one","title":"two"}',
    '{"nested":{"key":1,"key":2}}',
    '{"price":NaN}',
    '{"price":-Infinity}',
    '{"price":1e999}',
    '{"nested":{"price":-1e999}}',
):
    try:
        client.claim_dashboard_pending_write(
            scope='create',resource_key='create',
            idempotency_key='uid:hostile:json',method='POST',
            path='/api/write/ads',payload_json=invalid,ad_id=None,
        )
    except StorageOwnerError:
        pass
    else:
        raise AssertionError('executor persisted unreadable UI pending JSON')
    assert client.dashboard_pending_writes()==()
print('REAL_UID_HOSTILE_PENDING_JSON_FENCES_DENIED',flush=True)
assert not client.has_write_recovery_clearance(
    idempotency_key='uid:test:1', request_sha256='a'*64)
assert client.read_request('uid:test:1') is None
now=datetime(2026,10,11,12,0,tzinfo=timezone.utc)
assert client.tracked_ad_ids()==()
seed_rx=ReactionSnapshot(
    ad_id='777', observed_at=now, source='kleinanzeigen:email-only',
    conversation_count=2, unique_buyer_count=3, inbound_message_count=5,
)
assert client.reaction_history('777')==(seed_rx,)
assert client.reaction_history('unknown')==()
from mark_api.domain import InboundMessageEvent, AdClassification
from mark_api.query import MarkQueryService
from mark_api.analytics import AnalyticsService
events=(
    InboundMessageEvent(
        ad_id='777', conversation_id='buyer1',
        provider_message_id='real-uid-event-1',
        observed_at=now, source='email-only-synthetic',
    ),
    InboundMessageEvent(
        ad_id='777', conversation_id='buyer2',
        provider_message_id='real-uid-event-2',
        observed_at=datetime(2026,10,11,12,1,tzinfo=timezone.utc),
        source='email-only-synthetic',
    ),
)
assert client.inbound_message_history('777')==events
assert client.inbound_message_counts('777')==(2,2)
assert client.inbound_message_ad_ids()==('777',)
assert client.latest_classification('777')==AdClassification(
    ad_id='777', observed_at=now, source='manual', city='Berlin',
)
query=MarkQueryService(client)
assert query.email_reaction('777').conversation_count==2
assert query.email_reaction('777').inbound_message_count==2
analytics=AnalyticsService(client)
assert len(analytics.rank_ads('email_inbound_message_count'))==1
assert analytics.rank_ads('email_inbound_message_count')[0].value==2
groups=analytics.group_rankings('city','email_inbound_message_count')
assert len(groups)==1 and groups[0].label=='Berlin'
assert groups[0].metric_sum==2 and groups[0].sample_size==1
classification=client.merge_classification(
    ad_id='777', source='manual-uid-50044',
    changes={'title_type':'clear_text'},
    observed_at=now+timedelta(minutes=2),
)
assert classification.source=='manual-uid-50044'
assert classification.city=='Berlin' and classification.title_type=='clear_text'
assert client.latest_classification('777')==classification
batch=tuple(InboundMessageEvent(
    ad_id='779', conversation_id=f'new-buyer-{i % 3}',
    provider_message_id=f'uid-staged-email-{i:03}',
    observed_at=now+timedelta(minutes=i), source='email-uid50044',
) for i in range(19))
assert client.append_inbound_message_events(batch)==19
assert client.inbound_message_history('779')==batch
assert client.inbound_message_counts('779')==(3,19)
assert client.append_inbound_message_events(batch)==0
assert client.inbound_message_ad_ids()==('777','779')
assert len(client.inbound_message_history('779'))==19
print('REAL_UID_ATOMIC_INBOX_IMPORT_GREEN',flush=True)
rx_added=ReactionSnapshot(
    ad_id='777', observed_at=now+timedelta(minutes=3),
    source='uid50044-synthetic-reaction',
    conversation_count=3, unique_buyer_count=4,
    inbound_message_count=6,
)
assert client.append_reaction_snapshot(rx_added) is None
assert client.reaction_history('777')==(seed_rx,rx_added)
print('REAL_UID_TYPED_REACTION_APPEND_GREEN',flush=True)
print('REAL_UID_TYPED_CLASSIFICATION_MERGE_GREEN',flush=True)
print('REAL_UID_EMAIL_ANALYTICS_QUERY_GREEN',flush=True)


# Exercise the newly installed owner-only inventory composition under REAL
# UID50044 -> UID50046 SO_PEERCRED, without either a DB mount or a browser
# platform action. The fake reader observes empty ads but has no Write API.
from mark_api.owner_inventory_cli import sync_owner_inventory
class SyntheticInventoryReader:
    def __init__(self):
        self.reads=0
        self.closed=False
    def read_inventory(self):
        self.reads+=1
        return ReadResult.success_empty()
    def close(self):
        self.closed=True
synthetic_reader=SyntheticInventoryReader()
owner_only=sync_owner_inventory(
    client, cdp_port=9222, clock=lambda:now,
    runtime_factory=lambda **kw:synthetic_reader,
)
assert synthetic_reader.closed and synthetic_reader.reads==1
assert owner_only.observed_count==0 and owner_only.persisted_count==0
assert client.sync_status()['state']=='success_empty'
assert client.sync_status()['last_successful_attempt']['source']=='private-web-owner-inventory'
assert not Path('/data/mark.sqlite').exists()
print('REAL_UID_OWNER_ONLY_INVENTORY_CLI_GREEN',flush=True)

sync=client.begin_sync_attempt(source="docker_synthetic",started_at=now)
ad_snapshots=tuple(
    AdSnapshot(ad_id=str(800+index),
               source=("management-observed" if index==8 else "docker_synthetic"),
               observed_at=now,lifecycle_state=LifecycleState.ACTIVE,
               title="Synthetic",description="Line one\\n"+"x"*7800,
               views=index)
    for index in range(9))
assert client.append_inventory_result(
    ReadResult.success_nonempty(ad_snapshots),tracked_ad_ids=(),
    observed_at=now,source="docker_synthetic",attempt_id=sync,
    completed_at=now)==9
assert client.tracked_ad_ids()==tuple(str(800+i) for i in range(9))
assert client.ad_history('808')==(ad_snapshots[-1],)
assert client.latest_ad_snapshot('800')==ad_snapshots[0]
assert client.ad_history('missing')==()
assert client.sync_status()["state"]=="success_nonempty"
assert client.ad_history('808')[0].source=="management-observed"
assert client.sync_status()["last_successful_attempt"]["source"]=="docker_synthetic"
assert not Path('/data/mark.sqlite').exists(), 'executor unexpectedly has SQLite volume'
from mark_api.dashboard import create_server, DashboardWriteProxy
from threading import Thread
from urllib.request import urlopen, Request
from urllib.error import HTTPError
dashboard=create_server(client,port=0)
dashboard_thread=Thread(target=dashboard.serve_forever,daemon=True)
dashboard_thread.start()
dashboard_host,dashboard_port=dashboard.server_address
dashboard_origin=f'http://{dashboard_host}:{dashboard_port}'
try:
    def get_api(route):
        with urlopen(dashboard_origin+route,timeout=4) as response:
            assert response.status==200
            return json.loads(response.read())
    assert get_api('/readyz')=={'status':'ready'}
    ads=get_api('/api/ads')
    assert len(ads)==9 and ads[0]['ad_id']=='800',ads
    summary=get_api('/api/summary')
    assert summary['tracked_ads']==9
    assert summary['sync_recovery']['unresolved_count']==0
    assert get_api('/api/sync/recovery')==summary['sync_recovery']
    assert get_api('/api/dashboard/config')=={'write_ui_available':False}
    rankings=get_api('/api/analytics/ads?metric=views')
    assert len(rankings)==9 and rankings[0]['ad_id']=='808',rankings
    labels=get_api('/api/analytics/groups?dimension=city&metric=email_inbound_message_count')
    assert len(labels)==1 and labels[0]['label']=='Berlin',labels
    try:
        urlopen(Request(dashboard_origin+'/api/write/ads/800',
                        data=b'{"title":"denied"}',method='PATCH'),timeout=4)
    except HTTPError as error:
        assert error.code==405,error.code
    else:
        raise AssertionError('socket-backed dashboard opened unmediated HTTP Write')
    try:
        other=create_server(
            client,port=0,
            write_proxy=DashboardWriteProxy(
                host='127.0.0.1',port=9,
                bearer_token='backend-token-00000000001',
                ui_token='dashboard-token-000000001',
            ),
        )
    except ValueError:
        pass
    else:
        other.server_close()
        raise AssertionError('socket-backed dashboard enabled Write proxy')
finally:
    dashboard.shutdown()
    dashboard.server_close()
    dashboard_thread.join(timeout=4)
print('REAL_UID_DASHBOARD_HTTP_OWNER_NO_DB_MOUNT_GREEN',flush=True)
request=client.claim(key='uid:test:1',sha='a'*64,owner='runtime:one',at=now)
assert request.created and isinstance(request.record,WriteApiRequestRecord)
started=client.begin(key='uid:test:1',sha='a'*64,owner='runtime:one',at=now)
assert started.state=='in_progress' and started.execution_started_at is not None
assert not client.has_write_recovery_clearance(
    idempotency_key='uid:test:1',request_sha256='a'*64)
assert client.create_pending(exclude_key='uid:test:next',owner='runtime:one') is True
# Typed RPC must not be a SQL, arbitrary method, completion or clearance broker.
for operation in (
    'clearance','record_write_recovery_clearance',
    'complete','SQL','execute','restore','delete',
):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.connect('/rpc/owner.sock')
        payload={'version':1,'op':operation,'key':'uid:test:1',
                 'sql':'DELETE FROM write_api_requests'}
        sock.sendall((json.dumps(payload)+'\n').encode())
        received=b''
        while not received.endswith(b'\n'):
            part=sock.recv(4096)
            assert part
            received+=part
        reply=json.loads(received)
        assert reply=={'ok':False,'error':'request_denied'},reply
assert client.create_pending(exclude_key='uid:test:next',owner='runtime:one')
# Old successful completion cannot be forged by the compromised runtime.
assert client.read_request('uid:test:1').state=='in_progress'
# A lost ACK is UNKNOWN: only perform observation-only readback, not replay.
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
    sock.settimeout(4)
    sock.connect('/rpc/owner.sock')
    request={'version':1,'op':'claim','key':'uid:lost-ack','sha':'b'*64,
             'owner':'runtime:one','at':now.isoformat()}
    sock.sendall((json.dumps(request)+'\n').encode())
    sock.shutdown(socket.SHUT_RDWR)
for _ in range(100):
    record=client.read_request('uid:lost-ack')
    if record is not None:break
    time.sleep(0.02)
assert record is not None and record.request_sha256=='b'*64
# Start a new generation and stage, but intentionally do not commit it.
# SIGKILL below must retain this attempt as pending, not publish its bytes.
incomplete=client.begin_sync_attempt(source='docker_synthetic',started_at=now)
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
    sock.settimeout(4)
    sock.connect('/rpc/owner.sock')
    candidate=_snapshot_as_json(AdSnapshot(
        ad_id='999',source='docker_synthetic',observed_at=now,
        lifecycle_state=LifecycleState.ACTIVE,
    ))
    payload={'version':1,'op':'sync_stage','attempt':incomplete,
             'offset':0,'snapshots':[candidate]}
    sock.sendall((json.dumps(payload)+'\n').encode())
    received=b''
    while not received.endswith(b'\n'):
        received+=sock.recv(4096)
    assert json.loads(received)['ok'] is True
assert client.sync_stage_status(incomplete)['accepted']==1
assert client.sync_status()['state']=='in_progress'
pending_recovery=client.sync_recovery_status()
assert pending_recovery['unresolved_count']==1
assert pending_recovery['oldest_unresolved_attempt']['id']==incomplete
print('STORAGE_TYPED_FENCE_AND_LOST_ACK_GREEN',flush=True)
"""

_FOREIGN_DAC = r"""
import os, mmap
from pathlib import Path
db=Path('/data')
denied=0
for name in ('mark.sqlite','mark.sqlite-wal','mark.sqlite-shm'):
    path=db/name
    for flags in (os.O_RDONLY,os.O_RDWR):
        try:
            fd=os.open(path,flags)
        except PermissionError:
            denied+=1
            continue
        except FileNotFoundError:
            # A temporary WAL/SHM inode may not exist between transactions.
            assert name != 'mark.sqlite', 'main database unexpectedly missing'
            continue
        else:
            os.close(fd)
            raise AssertionError('UID50044 opened private SQLite inode '+name)
assert denied>=2, 'private existing main database was not DAC-denied'
print('EXECUTOR_RAW_SQLITE_FD_MAPPING_DENIED',flush=True)
"""

_PERSISTED_AFTER_KILL = r"""
import sqlite3
from pathlib import Path
database=Path('/data/mark.sqlite')
db=sqlite3.connect('file:/data/mark.sqlite?mode=ro',uri=True)
try:
    persisted=db.execute(
        'SELECT state,request_sha256,execution_started_at FROM write_api_requests '
        'WHERE idempotency_key=?',('uid:test:1',)
    ).fetchone()
    assert persisted is not None
    assert persisted[0]=='in_progress' and persisted[1]=='a'*64
    assert persisted[2] is not None
    late=db.execute('SELECT request_sha256 FROM write_api_requests '
                    'WHERE idempotency_key=?',('uid:lost-ack',)).fetchone()
    assert late is not None and late[0]=='b'*64
    cleared=db.execute('SELECT COUNT(*) FROM write_recovery_clearances').fetchone()
    assert cleared[0]==0
    correct=db.execute("SELECT COUNT(*) FROM sync_attempts WHERE outcome='success_nonempty'").fetchone()
    pending=db.execute("SELECT COUNT(*) FROM sync_attempts WHERE outcome='in_progress'").fetchone()
    assert correct[0]==1 and pending[0]==1
    rows=db.execute("SELECT COUNT(*) FROM ad_snapshots WHERE ad_id IN "
                    "('800','801','802','803','804','805','806','807','808')").fetchone()
    assert rows[0]==9
    persisted_rx=db.execute(
        "SELECT conversation_count,unique_buyer_count,inbound_message_count "
        "FROM reaction_snapshots WHERE ad_id='777' "
        "AND source='uid50044-synthetic-reaction'"
    ).fetchall()
    assert persisted_rx==[(3,4,6)],persisted_rx
    uncommitted=db.execute("SELECT COUNT(*) FROM ad_snapshots WHERE ad_id='999'").fetchone()
    assert uncommitted[0]==0
finally:
    db.close()
print('SIGKILL_PERSISTED_OPEN_FENCES_AND_NO_CLEARANCE',flush=True)
"""


_STALE_SOCKET_OPERATOR_CLEANUP = r"""
from pathlib import Path
import os,stat
socket=Path('/rpc/owner.sock')
identity=socket.lstat()
assert os.geteuid()==50046
assert identity.st_uid==50046 and identity.st_gid==50044
assert stat.S_ISSOCK(identity.st_mode)
socket.unlink()
print('TRUSTED_OWNER_CLEANED_ONLY_STALE_TEST_SOCKET',flush=True)
"""

_OWNER_RESTART_RECOVERY = r"""
from datetime import datetime,timezone
from pathlib import Path
from mark_api.storage_owner import StorageOwnerClient,StorageOwnerError
client=StorageOwnerClient(Path('/rpc/owner.sock'),expected_owner_uid=50046)
assert client.ready()
recovery=client.sync_recovery_status()
assert recovery['unresolved_count']==1,recovery
old=recovery['oldest_unresolved_attempt']
assert old['source']=='docker_synthetic' and old['outcome']=='in_progress'
assert client.sync_status()['state']=='in_progress'
try:
    client.begin_sync_attempt(
        source='new-generation',started_at=datetime(2026,10,12,19,0,tzinfo=timezone.utc),
    )
except StorageOwnerError:
    pass
else:
    raise AssertionError('new generation bypassed durable sync quarantine')
assert client.sync_recovery_status()==recovery
assert not client.has_write_recovery_clearance(
    idempotency_key='uid:test:1',request_sha256='a'*64,
)
print('REAL_UID_RESTART_PRESERVES_DURABLE_SYNC_QUARANTINE',flush=True)
"""


_WRONG_UID = r"""
from pathlib import Path
from mark_api.storage_owner import StorageOwnerClient,StorageOwnerError
try:
    StorageOwnerClient(Path('/rpc/owner.sock'),expected_owner_uid=50047).ready()
except StorageOwnerError:
    print('FORGED_OWNER_UID_REJECTED',flush=True)
else:
    raise AssertionError('forged storage owner uid attestation')
"""


_INSTALLED_NONEMPTY = r"""
from datetime import datetime, timezone
from pathlib import Path
from importlib import metadata
import subprocess
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.results import ReadResult
from mark_api.storage_owner import StorageOwnerClient
from mark_api import owner_inventory_cli as cli

assert "site-packages" in Path(cli.__file__).resolve().parts
distribution=metadata.distribution("mark-api")
assert distribution.version=="0.1.0"
assert "mark-api-inventory-sync" in {
    entry.name for entry in distribution.entry_points
    if entry.group=="console_scripts"
}
probe=subprocess.run(
    ["/opt/mark-api/venv/bin/mark-api-inventory-sync","--help"],
    capture_output=True,text=True,timeout=10,
)
assert probe.returncode==2 and "unattested container draft" in probe.stderr
client=StorageOwnerClient(Path("/rpc/owner.sock"),expected_owner_uid=50046)
assert client.ready()
assert client.sync_recovery_status()=={
    'unresolved_count':0,'oldest_unresolved_attempt':None,
}
now=datetime(2026,10,12,19,0,tzinfo=timezone.utc)
snapshot=AdSnapshot(
    ad_id="910",source="installed-wheel-synthetic-reader",
    observed_at=now,lifecycle_state=LifecycleState.ACTIVE,
    title="Synthetic installed owner",description="No platform connection",
)
class SyntheticReader:
    reads=0
    closed=False
    def read_inventory(self):
        self.reads+=1
        return ReadResult.success_nonempty((snapshot,))
    def close(self):
        self.closed=True
reader=SyntheticReader()
receipt=cli.sync_owner_inventory(
    client,cdp_port=9222,clock=lambda:now,
    runtime_factory=lambda **kw:reader,
)
assert reader.reads==1 and reader.closed
assert receipt.observed_count==1 and receipt.persisted_count==1
assert client.tracked_ad_ids()==("910",)
assert client.ad_history("910")==(snapshot,)
status=client.sync_status()
assert status["state"]=="success_nonempty"
assert status["latest_attempt"]["snapshot_count"]==1
assert status["latest_attempt"]["source"]=="private-web-owner-inventory"
assert not Path("/data/mark.sqlite").exists()
claim=client.claim(
    key="installed:uid:request",sha="c"*64,owner="runtime:installed",at=now,
)
assert claim.created
started=client.begin(
    key="installed:uid:request",sha="c"*64,owner="runtime:installed",at=now,
)
assert started.state=="in_progress" and started.execution_started_at is not None
assert client.read_request("installed:uid:request")==started
assert not client.has_write_recovery_clearance(
    idempotency_key="installed:uid:request",request_sha256="c"*64,
)
print("REAL_UID_INSTALLED_WHEEL_NONEMPTY_OWNER_CLI_AND_DRAFT_GATE_GREEN",flush=True)
"""


@unittest.skipUnless(IMAGE_OK, "Explicit immutable preexisting Docker test image needed")
class RealStorageOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        _require_exclusive_docker_admin_authority()

    def setUp(self) -> None:
        super().setUp()
        _require_exclusive_docker_admin_authority()

    @unittest.skipUnless(
        PYTHON == "/opt/mark-api/venv/bin/python",
        "requires exact installed Mark release-image Python",
    )
    def test_installed_wheel_nonempty_owner_inventory_and_draft_gate(self) -> None:
        _require_exclusive_docker_admin_authority()
        # Separate source-free container path; the usual multi-UID test binds
        # /src and cannot attest whether the release Wheel includes the CLI.
        suffix = uuid4().hex[:12]
        store_volume = "mark-installed-owner-db-" + suffix
        socket_volume = "mark-installed-owner-rpc-" + suffix
        service = "mark-installed-owner-" + suffix
        volumes: list[str] = []
        started = False
        code = ["--workdir", "/", "--entrypoint", PYTHON]
        store = ["--mount", f"type=volume,source={store_volume},target=/data"]
        rpc = ["--mount", f"type=volume,source={socket_volume},target=/rpc"]
        try:
            for name in (store_volume, socket_volume):
                _docker("volume", "create", name)
                volumes.append(name)
            seeded = _docker(
                "run", "--rm", "--pull", "never", "--network", "none",
                "--user", "0:0", *code, *store, *rpc,
                IMAGE, "-I", "-B", "-c", _SEED,
            )
            self.assertIn("PRIVATE_EXISTING_SCHEMA_V2", seeded.stdout)
            _docker(
                "run", "-d", "--pull", "never", "--name", service,
                "--network", "none", "--user", "50046:50046", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                *code, *store, *rpc, IMAGE, "-I", "-B", "-c", _SERVICE,
            )
            started = True
            self._ready(service, "PRIVATE_STORAGE_OWNER_READY")
            executor = _docker(
                "run", "--rm", "--pull", "never", "--network", "none",
                "--read-only", "--user", "50044:50044", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                *code, *rpc, IMAGE, "-I", "-B", "-c", _INSTALLED_NONEMPTY,
            )
            self.assertIn(
                "REAL_UID_INSTALLED_WHEEL_NONEMPTY_OWNER_CLI_AND_DRAFT_GATE_GREEN",
                executor.stdout,
            )
        finally:
            if started:
                stopped = _docker("rm", "-f", service, allow_failure=True)
                self.assertEqual(stopped.returncode, 0, stopped.stderr[-300:])
            for volume in reversed(volumes):
                deleted = _docker("volume", "rm", volume, allow_failure=True)
                self.assertEqual(deleted.returncode, 0, deleted.stderr[-300:])

    def test_foreign_uid_cannot_rewrite_or_clear_private_sqlite_fences(self) -> None:
        _require_exclusive_docker_admin_authority()
        suffix = uuid4().hex[:12]
        store_volume = "mark-store-owner-db-" + suffix
        socket_volume = "mark-store-owner-rpc-" + suffix
        service = "mark-store-owner-" + suffix
        volumes: list[str] = []
        container_started = False
        with tempfile.TemporaryDirectory(prefix="mark-storage-owner-real-") as temp:
            root = Path(temp)
            source = root / "src"
            shutil.copytree(
                REPO / "src" / "mark_api", source / "mark_api",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for item in (root, source, *source.rglob("*")):
                if item.is_dir():
                    item.chmod(0o755)
                elif item.is_file():
                    item.chmod(0o644)
            code = [
                "--mount", f"type=bind,source={source},target=/src,readonly",
                "-e", "PYTHONPATH=/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "--workdir", "/", "--entrypoint", PYTHON,
            ]
            store = ["--mount", f"type=volume,source={store_volume},target=/data"]
            rpc = ["--mount", f"type=volume,source={socket_volume},target=/rpc"]
            try:
                for name in (store_volume, socket_volume):
                    _docker("volume", "create", name)
                    volumes.append(name)
                seed = _docker("run", "--rm", "--network", "none",
                               "--user", "0:0", *code, *store, *rpc,
                               IMAGE, "-B", "-c", _SEED)
                self.assertIn("PRIVATE_EXISTING_SCHEMA_V2", seed.stdout)
                _docker(
                    "run", "-d", "--name", service, "--network", "none",
                    "--user", "50046:50046", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    *code, *store, *rpc, IMAGE, "-B", "-c", _SERVICE,
                )
                container_started = True
                self._ready(service, "PRIVATE_STORAGE_OWNER_READY")
                runtime = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50044:50044", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code, *rpc, IMAGE, "-B", "-c", _RUNTIME,
                )
                self.assertIn("REAL_UID_OWNER_ONLY_INVENTORY_CLI_GREEN", runtime.stdout)
                self.assertIn("REAL_UID_HOSTILE_PENDING_JSON_FENCES_DENIED",
                              runtime.stdout)
                self.assertIn("STORAGE_TYPED_FENCE_AND_LOST_ACK_GREEN", runtime.stdout)
                self.assertIn("REAL_UID_DASHBOARD_HTTP_OWNER_NO_DB_MOUNT_GREEN",
                              runtime.stdout)
                # This additional mount exists ONLY for the hostile DAC test.
                hostile = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50044:50044", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code,
                    "--mount", f"type=volume,source={store_volume},target=/data,readonly",
                    IMAGE, "-B", "-c", _FOREIGN_DAC,
                )
                self.assertIn("EXECUTOR_RAW_SQLITE_FD_MAPPING_DENIED", hostile.stdout)
                wrong = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50044:50044", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code, *rpc, IMAGE, "-B", "-c", _WRONG_UID,
                )
                self.assertIn("FORGED_OWNER_UID_REJECTED", wrong.stdout)
                # SIGKILL: an old socket must not be silently deleted on restart.
                _docker("rm", "-f", service)
                container_started = False
                replay = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50046:50046", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code,
                    "--mount", f"type=volume,source={store_volume},target=/data,readonly",
                    IMAGE, "-B", "-c", _PERSISTED_AFTER_KILL,
                )
                self.assertIn("SIGKILL_PERSISTED_OPEN_FENCES_AND_NO_CLEARANCE",
                              replay.stdout)
                bad = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50046:50046", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code, *store, *rpc, IMAGE, "-B", "-c", _SERVICE,
                    allow_failure=True,
                )
                self.assertNotEqual(bad.returncode, 0)
                self.assertIn("refusing to replace storage RPC socket", bad.stderr)

                # This is an isolated, uniquely named synthetic RPC volume.
                # Only after docker rm -f confirmed the old service ended may
                # UID50046 remove its stale socket; UID50044 cannot do this.
                cleaned = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50046:50046", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code, *rpc, IMAGE, "-B", "-c", _STALE_SOCKET_OPERATOR_CLEANUP,
                )
                self.assertIn(
                    "TRUSTED_OWNER_CLEANED_ONLY_STALE_TEST_SOCKET", cleaned.stdout,
                )
                _docker(
                    "run", "-d", "--name", service, "--network", "none",
                    "--user", "50046:50046", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    *code, *store, *rpc, IMAGE, "-B", "-c", _SERVICE,
                )
                container_started = True
                self._ready(service, "PRIVATE_STORAGE_OWNER_READY")
                recovered = _docker(
                    "run", "--rm", "--network", "none", "--read-only",
                    "--user", "50044:50044", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true",
                    *code, *rpc, IMAGE, "-B", "-c", _OWNER_RESTART_RECOVERY,
                )
                self.assertIn(
                    "REAL_UID_RESTART_PRESERVES_DURABLE_SYNC_QUARANTINE",
                    recovered.stdout,
                )
            finally:
                if container_started:
                    result = _docker("rm", "-f", service, allow_failure=True)
                    self.assertEqual(result.returncode, 0, result.stderr[-300:])
                for volume in reversed(volumes):
                    result = _docker("volume", "rm", volume, allow_failure=True)
                    self.assertEqual(result.returncode, 0, result.stderr[-300:])

    def _ready(self, container: str, marker: str) -> None:
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            found = _docker("logs", container)
            if marker in found.stdout:
                return
            if "Traceback" in found.stderr + found.stdout:
                self.fail("synthetic storage service failed: " + found.stdout[-600:])
            time.sleep(0.2)
        self.fail("synthetic storage service did not start")



class DockerAuthorityGuardTests(unittest.TestCase):
    """Mock-only shared-daemon safety checks; no real Docker operation."""

    def test_mutation_aliases_block_before_subprocess(self) -> None:
        for args in (
            ("volume", "rm", "decoy"), ("volume", "remove", "decoy"),
            ("volume", "prune"), ("rm", "-f", "decoy"),
            ("container", "rm", "decoy"), ("network", "rm", "decoy"),
            ("system", "prune", "--volumes"), ("run", "--rm", "fake-image"),
        ):
            with self.subTest(args=args):
                with patch.object(subprocess, "run") as dispatched:
                    with self.assertRaisesRegex(
                        AssertionError, "exclusive Docker-admin authority",
                    ):
                        _docker(*args)
                    dispatched.assert_not_called()

    def test_passive_metadata_and_direct_testcase_gates(self) -> None:
        for args in (
            ("info",), ("image", "inspect", "sha256:" + "0" * 64),
            ("container", "inspect", "decoy"), ("volume", "inspect", "decoy"),
        ):
            fake = subprocess.CompletedProcess(["/usr/bin/docker", *args], 0, "", "")
            with patch.object(subprocess, "run", return_value=fake) as dispatched:
                self.assertIs(_docker(*args), fake)
                dispatched.assert_called_once()
        with patch.dict(os.environ, {"MARK_REQUIRE_STORAGE_OWNER_UID": "1"}):
            with self.assertRaisesRegex(
                AssertionError, "exclusive Docker-admin authority",
            ):
                _require_exclusive_docker_admin_authority()
        with patch.dict(os.environ, {"MARK_REQUIRE_STORAGE_OWNER_UID": "0"}):
            with patch.object(RealStorageOwnerTests, "__unittest_skip__", False, create=True):
                case = RealStorageOwnerTests("test_foreign_uid_cannot_rewrite_or_clear_private_sqlite_fences")
                result = case.run(unittest.TestResult())
        self.assertEqual(len(result.skipped), 1, result.skipped)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)