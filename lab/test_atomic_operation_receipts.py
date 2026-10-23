"""Offline SDK/protobuf/SQL-contract checks; no database or model calls."""
import copy
import json
import struct
import subprocess
import sys
import unittest
from types import SimpleNamespace

from bootstrap import ROOT
sys.path.insert(0, str(ROOT / "backend"))

from immudb.grpc import schema_pb2
from immudb.handler import sqlexec
from common.immudbIO.errors import UnconfirmedWriteError
from common.immudbIO import recoverable as module


def cell(row=0, column=0):
    return dict(cell_id=f"legacy-{row}-{column}", worksheet_id="cells", r=row, c=column,
                ctfa="0.00", ctt="n", cts="12.50", bg="#AABBCC", ff="7", fc="#112233",
                fs=12, bl=True, it=False, cl=False, un=True, vt=1, ht=2, tb=1, tr=90)


def proof(receipt, tx_id=42):
    # Server encodeRowValue stores a u32 count, then ordered u32 column id,
    # u32 UTF-8 byte length, bytes. Keep this fixture independent of the decoder.
    opid = receipt["operation_id"].encode("utf-8")
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw = (b"\x00\x00\x00\x02" + struct.pack(">II", 1, len(opid)) + opid
           + struct.pack(">II", 2, len(payload)) + payload)
    return SimpleNamespace(verified=True, PKIDs=[1], ColNamesById={1: "opid", 2: "receipt"},
        ColTypesById={1: "VARCHAR", 2: "VARCHAR"}, ColLenById={1: 64, 2: 4096},
        sqlEntry=SimpleNamespace(tx=tx_id, value=raw))


class SDKClient:
    def __init__(self):
        self.requests = []
        self.databases = []
        self.reads = []
        self.ack = schema_pb2.SQLExecResult(txs=[schema_pb2.CommittedSQLTx(
            header=schema_pb2.TxHeader(id=42), updatedRows=2)], ongoingTx=False)
        self.read_proof = None
        self.failure = None

    def useDatabase(self, name):
        self.databases.append(name)

    def sqlExec(self, sql, params=None):
        def execute(request):
            self.requests.append(request)
            if request.sql.startswith("CREATE TABLE"):
                return schema_pb2.SQLExecResult()
            if self.failure:
                raise self.failure
            return self.ack
        # Exercise installed 1.4.1 NamedParam conversion and its raw protobuf
        # return contract rather than substituting an invented response shape.
        return sqlexec._call_with_executor(sql, params or {}, False, execute)

    def verifiableSQLGet(self, table, keys):
        self.reads.append((table, keys))
        if isinstance(self.read_proof, Exception):
            raise self.read_proof
        return self.read_proof


class AtomicOperationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.args = dict(db_name="project", table_name="cells", operation_id="server-op-1",
                         scope_hash="1" * 64, request_hash="2" * 64,
                         effect_plan={"operation": "raw_upsert", "num_columns": 2, "cells": [cell()]})
        self.client = SDKClient()
        self.expected = module.build_receipt(**self.args)
        self.client.read_proof = proof(self.expected)

    def execute(self, plan=None):
        args = {**self.args, "effect_plan": plan or self.args["effect_plan"]}
        self.expected = module.build_receipt(**args)
        self.client.read_proof = proof(self.expected)
        return module.execute_operation(self.client, **args)

    def read(self):
        return module.read_operation_receipt(self.client, db_name="project", expected_receipt=self.expected)

    def test_import_does_not_load_platform_configuration(self):
        script = (f"import sys; sys.path.insert(0,{str(ROOT / 'backend')!r}); "
                  "import common.immudbIO.recoverable; "
                  "assert 'properties' not in sys.modules; assert 'django.conf' not in sys.modules")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_raw_preserves_all_sdk_values_inside_single_transaction(self):
        module.ensure_receipt_table(self.client, db_name="project")
        self.assertEqual(self.execute(), {"tx_id": 42, "receipt": self.expected})
        ddl, request = self.client.requests
        self.assertTrue(ddl.sql.startswith("CREATE TABLE IF NOT EXISTS " + module.RECEIPT_TABLE))
        self.assertNotIn("BEGIN", ddl.sql)
        self.assertTrue(request.sql.startswith("BEGIN TRANSACTION;\nUPSERT INTO cells"))
        self.assertTrue(request.sql.endswith(";\nCOMMIT;"))
        self.assertEqual(request.sql.count("BEGIN TRANSACTION;"), 1)
        self.assertEqual(request.sql.count("COMMIT;"), 1)
        self.assertLess(request.sql.index("UPSERT INTO"), request.sql.index("INSERT INTO " + module.RECEIPT_TABLE))
        params = {parameter.name: parameter.value for parameter in request.params}
        self.assertEqual(params["cell_0_cell_id"].s, "legacy-0-0")
        self.assertEqual(params["cell_0_ctfa"].s, "0.00")
        self.assertEqual(params["cell_0_cts"].s, "12.50")
        self.assertTrue(params["cell_0_bl"].b)
        self.assertEqual(params["cell_0_tr"].n, 90)
        self.assertEqual(len(params), len(module.CELL_FIELDS) + 2)
        self.assertEqual(self.client.databases, [b"project", b"project", b"project"])
        self.assertEqual(self.client.reads[0][1][0].value, "server-op-1")

    def test_cells_and_append_compile_whole_batch_and_distinct_parameters(self):
        for operation in ("cells_upsert", "append_row"):
            with self.subTest(operation=operation):
                self.client = SDKClient()
                plan = {"operation": operation, "num_columns": 2, "cells": [cell(3, 0), cell(3, 1)]}
                if operation == "append_row":
                    plan["row_index"] = 3
                self.execute(plan)
                request = self.client.requests[-1]
                verb = "INSERT" if operation == "append_row" else "UPSERT"
                self.assertEqual(request.sql.count(f"{verb} INTO cells"), 2)
                self.assertIn("@cell_0_c", request.sql)
                self.assertIn("@cell_1_c", request.sql)
                self.assertEqual(len(self.client.requests), 1)

    def test_clear_empty_absent_and_multiple_updates_still_include_unique_receipt(self):
        for coordinates in ([], [{"row_index": 99, "column_index": 0}],
                            [{"row_index": 0, "column_index": 0}, {"row_index": 0, "column_index": 1}]):
            with self.subTest(coordinates=coordinates):
                self.client = SDKClient()
                self.execute({"operation": "clear", "num_columns": 2, "coordinates": coordinates})
                request = self.client.requests[-1]
                self.assertEqual(request.sql.count("UPDATE cells SET ctt="), len(coordinates))
                self.assertEqual(request.sql.count("INSERT INTO " + module.RECEIPT_TABLE), 1)
                for forbidden in ("UPSERT", "SELECT", "SET bg", "SET ctfa"):
                    self.assertNotIn(forbidden, request.sql)

    def test_delete_has_exact_bound_row_and_commit_receipt(self):
        self.execute({"operation": "delete_row", "num_columns": 2, "row_index": 7})
        request = self.client.requests[-1]
        self.assertIn("DELETE FROM cells WHERE r=@row_index;", request.sql)
        self.assertEqual(next(p.value.n for p in request.params if p.name == "row_index"), 7)

    def test_all_prepared_cells_are_checked_before_any_rpc(self):
        cases = []
        for mutation in (lambda p: p["cells"][1].pop("ctfa"),
                         lambda p: p["cells"][1].update(worksheet_id="other"),
                         lambda p: p["cells"][1].update(r=True),
                         lambda p: p["cells"][1].update(c=2),
                         lambda p: p["cells"][1].update(cell_id=p["cells"][0]["cell_id"])):
            plan = {"operation": "cells_upsert", "num_columns": 2, "cells": [cell(0, 0), cell(0, 1)]}
            mutation(plan)
            cases.append({**self.args, "effect_plan": plan})
        cases.append({**self.args, "table_name": "cells; DELETE FROM cells"})
        cases.append({**self.args, "effect_plan": {"operation": "append_row", "num_columns": 2 ** 62,
                                                  "row_index": 0, "cells": [cell()]}})
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    module.execute_operation(self.client, **args)
                self.assertEqual(self.client.requests, [])
                self.assertEqual(self.client.databases, [])

    def test_lost_ack_or_server_error_never_retries_or_reads_as_success(self):
        for failure in (TimeoutError("lost after commit"), ValueError("statement failed")):
            self.client = SDKClient()
            self.client.failure = failure
            with self.assertRaises(UnconfirmedWriteError) as caught:
                module.execute_operation(self.client, **self.args)
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(len(self.client.requests), 1)
            self.assertEqual(self.client.reads, [])

    def test_ack_must_be_one_completed_commit_before_proof_read(self):
        for ack in (schema_pb2.SQLExecResult(),
                    schema_pb2.SQLExecResult(txs=[schema_pb2.CommittedSQLTx(header=schema_pb2.TxHeader(id=42))], ongoingTx=True),
                    schema_pb2.SQLExecResult(txs=[schema_pb2.CommittedSQLTx()]),
                    schema_pb2.SQLExecResult(txs=[schema_pb2.CommittedSQLTx(), schema_pb2.CommittedSQLTx()])):
            self.client = SDKClient()
            self.client.ack = ack
            with self.assertRaises(UnconfirmedWriteError):
                module.execute_operation(self.client, **self.args)
            self.assertEqual(self.client.reads, [])

    def test_matching_receipt_proof_is_required_after_ack(self):
        for change in (lambda p: setattr(p, "verified", False),
                       lambda p: setattr(p.sqlEntry, "tx", 43),
                       lambda p: setattr(p.sqlEntry, "tx", True)):
            self.client = SDKClient()
            self.client.read_proof = proof(self.expected)
            change(self.client.read_proof)
            with self.assertRaises(UnconfirmedWriteError):
                module.execute_operation(self.client, **self.args)
            self.assertEqual(len(self.client.requests), 1)

    def test_same_verified_bytes_must_match_every_persisted_scope_field(self):
        for field, replacement in (("operation_id", "another"), ("scope_hash", "3" * 64),
                                   ("request_hash", "4" * 64), ("effect_hash", "5" * 64),
                                   ("db_name", "other"), ("table_name", "other"), ("operation", "clear")):
            with self.subTest(field=field):
                changed = {**self.expected, field: replacement}
                self.client.read_proof = proof(changed)
                with self.assertRaises(ValueError):
                    self.read()
        self.assertEqual(self.client.requests, [])

    def test_bad_schema_and_raw_framing_cannot_be_positive_proof(self):
        original = proof(self.expected)
        for change in (lambda p: setattr(p, "PKIDs", [2]),
                       lambda p: setattr(p, "ColNamesById", {1: "opid", 2: "other"}),
                       lambda p: setattr(p, "ColTypesById", {1: "VARCHAR", 2: "JSON"}),
                       lambda p: setattr(p.sqlEntry, "value", p.sqlEntry.value + b"\x00"),
                       lambda p: setattr(p.sqlEntry, "value", p.sqlEntry.value[:-1]),
                       lambda p: setattr(p.sqlEntry, "value", b"\x00\x00\x00\x01" + p.sqlEntry.value[4:])):
            self.client.read_proof = copy.deepcopy(original)
            change(self.client.read_proof)
            with self.assertRaises(ValueError):
                self.read()

    def test_absence_permission_and_unavailable_read_never_dispatch_mutation(self):
        for error in (LookupError("no receipt"), PermissionError("forbidden"), TimeoutError("read unavailable")):
            self.client.read_proof = error
            with self.assertRaises(type(error)):
                self.read()
            self.assertEqual(self.client.requests, [])
        with self.assertRaises(ValueError):
            module.read_operation_receipt(self.client, db_name="other", expected_receipt=self.expected)


if __name__ == "__main__":
    unittest.main()
