"""Opt-in atomic SQL mutations and positive commit receipts (immudb-py 1.4.1).

Callers provision the receipt table and supply a dedicated, logged-in client:
database selection/root state must not race another operation. Receipt DDL is
separate from the single business transaction. A missing/unreadable receipt
never proves that a write did not run.
"""
import hashlib
import json
import re
import struct

from immudb import datatypesv2

from common.immudbIO.errors import UnconfirmedWriteError


RECEIPT_TABLE = "agent_operation_receipts_v1"
CELL_FIELDS = (
    "cell_id", "worksheet_id", "r", "c", "ctfa", "ctt", "cts", "bg", "ff",
    "fc", "fs", "bl", "it", "cl", "un", "vt", "ht", "tb", "tr",
)
OPERATIONS = {"raw_upsert", "cells_upsert", "clear", "delete_row", "append_row"}
RECEIPT_FIELDS = {
    "version", "operation_id", "scope_hash", "request_hash", "db_name",
    "table_name", "effect_hash", "operation",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value, name, limit=64):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"Invalid {name}")
    return value


def _coordinate(value, name):
    if type(value) is not int or not 0 <= value < 2 ** 63:
        raise ValueError(f"Invalid {name}")
    return value


def _validate_receipt(receipt):
    if type(receipt) is not dict or set(receipt) != RECEIPT_FIELDS:
        raise ValueError("Invalid receipt fields")
    if type(receipt["version"]) is not int or receipt["version"] != 1:
        raise ValueError("Unsupported receipt version")
    for key in ("operation_id", "db_name", "table_name"):
        _text(receipt[key], key)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", receipt["table_name"]):
        raise ValueError("Invalid SQL table identifier")
    if receipt["table_name"].lower() == RECEIPT_TABLE:
        raise ValueError("Receipt table cannot be a business target")
    for key in ("scope_hash", "request_hash", "effect_hash"):
        if not isinstance(receipt[key], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
            raise ValueError(f"Invalid {key}")
    if not isinstance(receipt["operation"], str) or receipt["operation"] not in OPERATIONS:
        raise ValueError("Unsupported operation")
    if len(_json(receipt).encode("utf-8")) > 4096:
        raise ValueError("Receipt is too large")


def build_receipt(*, db_name, table_name, operation_id, scope_hash, request_hash, effect_plan):
    if type(effect_plan) is not dict or "operation" not in effect_plan:
        raise ValueError("Invalid effect plan")
    receipt = dict(version=1, operation_id=operation_id, scope_hash=scope_hash,
                   request_hash=request_hash, db_name=db_name, table_name=table_name,
                   effect_hash=canonical_hash(effect_plan), operation=effect_plan["operation"])
    _validate_receipt(receipt)
    _statements(table_name, effect_plan)  # Validate the entire plan before any RPC.
    return receipt


def _statements(table_name, plan):
    """Compile already serializer-validated, fully prepared SDK payloads."""
    operation = plan["operation"]
    required = {"operation", "num_columns"}
    required |= ({"cells", "row_index"} if operation == "append_row" else
                 {"cells"} if operation in {"raw_upsert", "cells_upsert"} else
                 {"coordinates"} if operation == "clear" else {"row_index"})
    if (set(plan) != required or type(plan["num_columns"]) is not int
            or not 1 <= plan["num_columns"] < 2 ** 63):
        raise ValueError("Invalid effect plan fields")
    statements, params = [], {}
    if operation in {"raw_upsert", "cells_upsert", "append_row"}:
        cells = plan["cells"]
        if type(cells) is not list or not cells or (operation == "raw_upsert" and len(cells) != 1):
            raise ValueError("Invalid prepared cells")
        coordinates, ids = set(), set()
        for index, cell in enumerate(cells):
            if type(cell) is not dict or set(cell) != set(CELL_FIELDS) or cell["worksheet_id"] != table_name:
                raise ValueError("Expected complete prepared SDK cell")
            row = _coordinate(cell["r"], "row")
            column = _coordinate(cell["c"], "column")
            cell_id = _text(cell["cell_id"], "cell_id")
            if column >= plan["num_columns"] or (row, column) in coordinates or cell_id in ids:
                raise ValueError("Duplicate or out-of-range prepared cell")
            coordinates.add((row, column))
            ids.add(cell_id)
            placeholders = []
            for field in CELL_FIELDS:
                parameter = f"cell_{index}_{field}"
                params[parameter] = cell[field]
                placeholders.append("@" + parameter)
            verb = "INSERT" if operation == "append_row" else "UPSERT"
            statements.append(f"{verb} INTO {table_name} ({','.join(CELL_FIELDS)}) VALUES ({','.join(placeholders)});")
        if operation == "append_row":
            row = _coordinate(plan["row_index"], "row_index")
            if len(cells) != plan["num_columns"] or any(r != row for r, _ in coordinates):
                raise ValueError("Append must contain one complete allocated row")
    elif operation == "clear":
        if type(plan["coordinates"]) is not list:
            raise ValueError("Invalid clear coordinates")
        for index, coordinate in enumerate(plan["coordinates"]):
            if type(coordinate) is not dict or set(coordinate) != {"row_index", "column_index"}:
                raise ValueError("Invalid clear coordinate")
            row = _coordinate(coordinate["row_index"], "row_index")
            column = _coordinate(coordinate["column_index"], "column_index")
            if column >= plan["num_columns"]:
                raise ValueError("Column out of range")
            params.update({f"r_{index}": row, f"c_{index}": column, f"ctt_{index}": "g", f"cts_{index}": ""})
            statements.append(f"UPDATE {table_name} SET ctt=@ctt_{index}, cts=@cts_{index} WHERE r=@r_{index} AND c=@c_{index};")
    elif operation == "delete_row":
        params["row_index"] = _coordinate(plan["row_index"], "row_index")
        statements.append(f"DELETE FROM {table_name} WHERE r=@row_index;")
    else:
        raise ValueError("Unsupported operation")
    return statements, params


def ensure_receipt_table(client, *, db_name):
    _text(db_name, "db_name")
    client.useDatabase(db_name.encode("utf-8"))
    client.sqlExec(f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} "
                   "(opid VARCHAR[64] NOT NULL, receipt VARCHAR[4096] NOT NULL, PRIMARY KEY opid);")


def _decode_receipt(proof):
    # Fixed two-VARCHAR row framing from the pinned server's encodeRowValue /
    # DecodeValue; deliberately not a general SQL decoder or an unverified query.
    if (proof.verified is not True or proof.PKIDs != [1]
            or proof.ColNamesById != {1: "opid", 2: "receipt"}
            or proof.ColTypesById != {1: "VARCHAR", 2: "VARCHAR"}
            or proof.ColLenById != {1: 64, 2: 4096}):
        raise ValueError("Receipt proof/schema mismatch")
    raw = proof.sqlEntry.value
    if type(raw) is not bytes or not 20 <= len(raw) <= 4180:
        raise ValueError("Invalid receipt row length")
    offset, fields = 4, {}
    if struct.unpack_from(">I", raw)[0] != 2:
        raise ValueError("Receipt must contain exactly two values")
    for expected_id, limit in ((1, 64), (2, 4096)):
        if offset + 8 > len(raw):
            raise ValueError("Truncated receipt row")
        column_id, length = struct.unpack_from(">II", raw, offset)
        offset += 8
        if column_id != expected_id or not 0 < length <= limit or offset + length > len(raw):
            raise ValueError("Invalid receipt column")
        fields[column_id] = raw[offset:offset + length].decode("utf-8")
        offset += length
    if offset != len(raw):
        raise ValueError("Trailing receipt bytes")
    receipt = json.loads(fields[2])
    _validate_receipt(receipt)
    if fields[1] != receipt["operation_id"] or fields[2] != _json(receipt):
        raise ValueError("Non-canonical receipt")
    return receipt


def read_operation_receipt(client, *, db_name, expected_receipt):
    """Return positive proof only; all absence/read/proof failures propagate."""
    _validate_receipt(expected_receipt)
    if expected_receipt["db_name"] != db_name:
        raise ValueError("Receipt database mismatch")
    client.useDatabase(db_name.encode("utf-8"))
    proof = client.verifiableSQLGet(
        RECEIPT_TABLE, [datatypesv2.PrimaryKeyVarCharValue(expected_receipt["operation_id"])],
    )
    receipt = _decode_receipt(proof)
    tx_id = proof.sqlEntry.tx
    if type(tx_id) is not int or tx_id <= 0 or receipt != expected_receipt:
        raise ValueError("Receipt does not match persisted operation")
    return {"tx_id": tx_id, "receipt": receipt}


def execute_operation(client, *, db_name, table_name, operation_id, scope_hash, request_hash, effect_plan):
    """Dispatch once; require both a complete commit ACK and matching proof."""
    receipt = build_receipt(db_name=db_name, table_name=table_name, operation_id=operation_id,
                            scope_hash=scope_hash, request_hash=request_hash, effect_plan=effect_plan)
    statements, params = _statements(table_name, effect_plan)
    params.update(receipt_opid=operation_id, receipt_payload=_json(receipt))
    statements.append(f"INSERT INTO {RECEIPT_TABLE} (opid,receipt) VALUES (@receipt_opid,@receipt_payload);")
    sql = "\n".join(["BEGIN TRANSACTION;", *statements, "COMMIT;"])
    try:
        client.useDatabase(db_name.encode("utf-8"))
        response = client.sqlExec(sql, params)
        if (response.ongoingTx is not False or len(response.txs) != 1
                or type(response.txs[0].header.id) is not int or response.txs[0].header.id <= 0):
            raise ValueError("Commit acknowledgement missing")
        result = read_operation_receipt(client, db_name=db_name, expected_receipt=receipt)
        if result["tx_id"] != response.txs[0].header.id:
            raise ValueError("Receipt and acknowledgement transactions differ")
        return result
    except Exception as exc:
        raise UnconfirmedWriteError() from exc
