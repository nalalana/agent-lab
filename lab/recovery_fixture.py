"""New opt-in recovery extraction and SDK instrumentation; old runners are unchanged."""
import ast
import json
import os
from uuid import uuid4

from run_realdb_integration import load_service
from run_table_boundary import SOURCE, load_writer

RECEIPT_TABLE = "agent_operation_receipts_v1"
METHODS = {"submit_recoverable_write", "reconcile_committed_write"}


def load_recovery_writer(remote, *, admin=True):
    writer = load_writer(remote)
    from apps.iam.models import Grant, Resource
    namespace = writer.upsert_table_cells.__globals__
    namespace.update(Resource=Resource, Grant=Grant)
    def construct():
        if hasattr(remote, "immudb_client"):
            remote.immudb_client.service_constructions += 1
        return remote
    namespace["ImmudbService"] = construct
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OpenGateService")
    selected = [n for n in cls.body if getattr(n, "name", None) in METHODS]
    if {n.name for n in selected} != METHODS:
        return writer  # Baseline is explicitly unsupported; never substitute an implementation.
    helpers = {"RecoverableWriteUnknown", "_recoverable_scope", "_check_recoverable_receipt", "_finish_recoverable_write"}
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body.extend(n for n in tree.body if getattr(n, "name", None) in helpers)
    cls.name, cls.body = "RecoveryMethods", selected
    body.append(cls)
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), "exec"), namespace)
    for name in METHODS:
        setattr(writer, name, staticmethod(getattr(namespace["RecoveryMethods"], name)))
    writer._has_iam_permission = staticmethod(lambda *_: admin)  # IAM remains an explicit fixture.
    return writer


def prepare_scope(user, gate):
    """Create only the two IAM model tables needed for real resource/tenant binding."""
    from django.db import connection
    from apps.iam.models import Tenant, Resource
    tables = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (Tenant, Resource):
            if model._meta.db_table not in tables:
                editor.create_model(model)
    tenant = Tenant.objects.create(type=Tenant.TYPE_PERSONAL, external_id=uuid4().hex, owner_user=user)
    return Resource.objects.create(type=Resource.TYPE_PROJECT, external_id=gate.gid, tenant=tenant)


def bind_table(table, project):
    from apps.iam.models import Resource
    return Resource.objects.create(type=Resource.TYPE_TABLE, external_id=table.tid,
        parent=project, tenant=project.tenant)


def logical_transaction(query, params):
    """Recognize the pinned prepared receipt transaction, excluding independent DDL."""
    if not isinstance(query, str) or not isinstance(params, dict):
        return False
    statements = [line.strip() for line in query.splitlines() if line.strip()]
    try:
        receipt = json.loads(params["receipt_payload"])
        return (len(statements) >= 4 and statements[0] == "BEGIN TRANSACTION;" and statements[-1] == "COMMIT;"
            and sum(line.startswith(f"INSERT INTO {RECEIPT_TABLE} ") for line in statements) == 1
            and statements[-2] == f"INSERT INTO {RECEIPT_TABLE} (opid,receipt) VALUES (@receipt_opid,@receipt_payload);"
            and receipt["operation_id"] == params["receipt_opid"]
            and any(line.startswith(("UPSERT INTO ", "UPDATE ", "DELETE FROM ", "INSERT INTO "))
                and not line.startswith(f"INSERT INTO {RECEIPT_TABLE} ") for line in statements[1:-2]))
    except (KeyError, TypeError, ValueError):
        return False


class RecoveryClient:
    """Count SDK calls and inject only after a real receipt transaction ACK."""
    def __init__(self, client):
        self.client, self.fault = client, None
        self.business_mutation_calls = self.receipt_reads = self.total_rpc = 0
        self.service_constructions = 0
        self.confirmed_transactions, self.last_operation_id = [], None
        self.entered = self.release = None
        self.proof_entered = self.proof_release = None
        self.last_injection = None

    def __getattr__(self, name):
        return getattr(self.client, name)

    def counters(self):
        return {key: getattr(self, key) for key in
                ("business_mutation_calls", "receipt_reads", "total_rpc", "service_constructions")}

    def useDatabase(self, *args, **kwargs):
        self.total_rpc += 1
        return self.client.useDatabase(*args, **kwargs)

    def sqlQuery(self, *args, **kwargs):
        self.total_rpc += 1
        return self.client.sqlQuery(*args, **kwargs)

    def verifiableSQLGet(self, *args, **kwargs):
        self.total_rpc += 1
        self.receipt_reads += 1
        if self.proof_entered is not None:
            entered, release = self.proof_entered, self.proof_release
            self.proof_entered = None
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Receipt proof read was not released")
        return self.client.verifiableSQLGet(*args, **kwargs)

    def sqlExec(self, query, params=None, *args, **kwargs):
        atomic = logical_transaction(query, params)
        fault = self.fault if atomic else None
        if atomic:
            self.last_operation_id = params["receipt_opid"]
            self.fault = None
            if fault == "before_dispatch":
                raise TimeoutError("Injected before receipt transaction dispatch")
            if fault == "pause":
                self.entered.set()
                if not self.release.wait(5):
                    raise TimeoutError("Receipt transaction was not released")
            if fault in {"partial", "request_digest_mismatch", "operation_id_mismatch", "scope_mismatch"}:
                params = dict(params)
                receipt = json.loads(params["receipt_payload"])
                field = {"partial": "effect_hash", "request_digest_mismatch": "request_hash",
                         "operation_id_mismatch": "operation_id", "scope_mismatch": "scope_hash"}[fault]
                receipt[field] = uuid4().hex if field == "operation_id" else "0" * 64
                params["receipt_payload"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                if fault == "partial":
                    lines = query.splitlines()
                    business = [i for i, line in enumerate(lines) if line.startswith("UPSERT INTO ")]
                    if len(business) != 2:
                        raise AssertionError("Partial fixture requires exactly two prepared cells")
                    del lines[business[1]]
                    query = "\n".join(lines)
                    params = {key: value for key, value in params.items() if not key.startswith("cell_1_")}
                self.last_injection = fault
            if fault == "abort":
                first = next(line for line in query.splitlines() if line.startswith("UPSERT INTO "))
                # Valid SQL fails on the already-created primary key after prior DML
                # and the receipt INSERT, testing actual transaction rollback.
                duplicate = first.replace("UPSERT INTO ", "INSERT INTO ", 1)
                query = query.removesuffix("COMMIT;") + duplicate + "\nCOMMIT;"
                self.last_injection = fault
        self.total_rpc += 1
        mutation = any(line.lstrip().upper().startswith(("UPSERT INTO ", "UPDATE ", "DELETE FROM "))
            or (line.lstrip().upper().startswith("INSERT INTO ")
                and not line.lstrip().upper().startswith(f"INSERT INTO {RECEIPT_TABLE.upper()} "))
            for line in query.splitlines())
        if atomic or mutation:
            self.business_mutation_calls += 1
        response = self.client.sqlExec(query, params, *args, **kwargs) if params is not None else self.client.sqlExec(query, *args, **kwargs)
        if atomic:
            if (getattr(response, "ongoingTx", None) is not False or len(getattr(response, "txs", [])) != 1
                    or type(response.txs[0].header.id) is not int or response.txs[0].header.id <= 0):
                raise AssertionError("Fault injection requires one real completed transaction")
            self.confirmed_transactions.append({"operation_id": self.last_operation_id, "tx_id": response.txs[0].header.id})
            if fault == "ack_lost":
                raise TimeoutError("Injected response loss after confirmed atomic receipt commit")
            if fault == "exit":
                os._exit(73)
        return response


class RecoveryRemote:
    """Wrap a dedicated logged-in client; the parent factory owns database/table cleanup."""
    def __init__(self, client, gid):
        self.gid = gid
        self.immudb_client = RecoveryClient(client)
        self.service = load_service(self.immudb_client)

    def get_data(self, *args):
        return self.service.get_data(*args)

    def values(self, tid):
        from common.immudbIO.client import toumingClient
        self.immudb_client.useDatabase(self.gid.encode())
        rows = toumingClient(self.immudb_client).simpleSelect(tid, ["r", "c", "cts"], {})
        if not isinstance(rows, list):
            raise ValueError("Read did not return rows")
        return sorted([{"r": r[0], "c": r[1], "value": r[2]} for r in rows], key=lambda r: (r["r"], r["c"]))
