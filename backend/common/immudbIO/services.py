"""
/backend/common/immudbIO/services.py
封装上层逻辑
"""
import os
import hashlib
import json
from datetime import datetime, timezone
from immudb import ImmudbClient
from common.immudbIO.models import Worksheet, Cell, CellCttChoices
from common.immudbIO.serializers import WriteDataSerializer
from common.immudbIO.client import toumingClient
from properties import db_local_base_url, db_local_base_port, USER, PASSWORD


CELL_SELECT_FIELDS = [
    "cell_id", "worksheet_id", "r", "c", "ctfa", "ctt", "cts", "bg", "ff",
    "fc", "fs", "bl", "it", "cl", "un", "vt", "ht", "tb", "tr",
]


class ImmudbService:
    def __init__(self):
        # 优先使用环境变量（Docker 环境），否则使用 properties 中的配置
        immudb_host = os.getenv('IMMUDB_HOST', db_local_base_url)
        immudb_port = os.getenv('IMMUDB_PORT', str(db_local_base_port))
        immudb_user = os.getenv('IMMUDB_USER', USER)
        immudb_password = os.getenv('IMMUDB_ADMIN_PASSWORD', PASSWORD)
        
        immudb_url = f"{immudb_host}:{immudb_port}"
        self.immudb_client = ImmudbClient(immudb_url)
        self.immudb_client.login(immudb_user, immudb_password)

    def create_db(self, gid: str):
        from common.immudbIO.serializers import CreateProjectSerializer

        # 序列化器验证
        serializer = CreateProjectSerializer(data={'gid': gid})
        if not serializer.is_valid():
            return False, serializer.errors
        
        # 创建数据库
        try:
            self.immudb_client.createDatabase(gid)
        except Exception as e:
            return False, f'创建失败: {str(e)}'

        # 创建项目表格
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
            tclient = toumingClient(self.immudb_client)
            tclient.createTestTable(gid, *Worksheet)
            return True, '创建成功'
        except Exception as e:
            return False, f'创建失败: {str(e)}'

    def delete_db(self, gid: str):
        from common.immudbIO.serializers import CreateProjectSerializer

        serializer = CreateProjectSerializer(data={'gid': gid})
        if not serializer.is_valid():
            return False, serializer.errors

        # immudb requires a database to be unloaded before it can be deleted.
        # Ignore an unload error here so an already-unloaded database can still
        # be deleted by the following call.
        try:
            self.immudb_client.unloadDatabase(gid)
        except Exception:
            pass

        try:
            self.immudb_client.deleteDatabase(gid)
            return True, '删除成功'
        except Exception as e:
            return False, f'删除失败: {str(e)}'

    def create_table(self, gid: str, tid: str, column: int):
        from common.immudbIO.serializers import CreateTableSerializer

        # 连接数据库
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
        except Exception as e:
            return False, f'数据库不存在: {str(e)}'
        
        # 序列化器验证
        serializer = CreateTableSerializer(data={'gid': gid, 'tid': tid, 'column': column})
        if not serializer.is_valid():
            return False, serializer.errors
        
        # 创建表格
        tclient = toumingClient(self.immudb_client)
        try:
            tclient.createTestTable(tid, *Cell)
        except Exception as e:
            return False, f'创建失败: {str(e)}'

        #向项目表格中添加表格信息
        success, message = tclient.insertToTable(
            gid, 
            fields=["worksheet_id", "default_column"], 
            values=["@worksheet_id", "@default_column"], 
            params={'worksheet_id': tid, 'default_column': column}
        )
        if not success:
            return False, message
        return True, '创建成功'

    def get_data(self, gid: str, tid: str, limit: int, offset: int):
        from common.immudbIO.serializers import GetDataSerializer

        # 序列化器验证
        serializer = GetDataSerializer(data={'gid': gid, 'tid': tid, 'limit': limit, 'offset': offset})
        if not serializer.is_valid():
            return False, serializer.errors
        
        # 连接数据库
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
        except Exception as e:
            return False, f'数据库不存在: {str(e)}'
        
        tclient = toumingClient(self.immudb_client)
        raw_result = tclient.simpleSelect(
            tid,
            ['*'],
            {},
            *[f"r >= {offset * limit} and r < {offset * limit + limit}"]
        )
        
        # Rows are addressed by r; COUNT(*) counts cells, not rows.
        summary = tclient.simpleSelect(tid, ['COUNT(*)', 'MAX(r)'], {})
        total = int(summary[0][0]) if summary else 0
        max_row_index = int(summary[0][1]) if total and summary[0][1] is not None else -1
        has_more = max_row_index >= (offset + 1) * limit
        
        return True, {
            'data': raw_result,
            'total': total,
            'has_more': has_more,
            'max_row_index': max_row_index,
        }

    def upsert_data(self, gid: str, tid: str, data: dict):
        from common.immudbIO.serializers import WriteDataSerializer
        serializer = WriteDataSerializer(data=data)
        if not serializer.is_valid():
            return False, serializer.errors

        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
        except Exception as e:
            return False, f'数据库不存在: {str(e)}'
        
        tclient = toumingClient(self.immudb_client)

        # New coordinates must have a stable primary key across writers.
        coordinate = f"{tid}:{serializer.validated_data['r']}:{serializer.validated_data['c']}"
        cell_id = hashlib.sha256(coordinate.encode('utf-8')).hexdigest()
        cell_data = tclient.simpleSelect(
            tid,
            ['cell_id'],
            {},
            *[f"r = {serializer.validated_data['r']} and c = {serializer.validated_data['c']}"]
        )
        if isinstance(cell_data, tuple) and cell_data and cell_data[0] is False:
            return False, cell_data[1]
        if len(cell_data) > 1:
            return False, '单元格坐标存在重复记录，请先修复数据'
        if cell_data:
            first_row = cell_data[0]
            if isinstance(first_row, dict):
                cell_id = first_row.get('cell_id') or cell_id
            else:
                cell_id = first_row[0] if len(first_row) > 0 else cell_id
        

        resp = tclient.upsertToTable(
            tid,
            ["cell_id", "worksheet_id", "r", "c", "ctfa", "ctt", "cts", "bg", "ff", "fc", "fs", "bl", "it", "cl", "un", "vt", "ht", "tb", "tr"],
            ["@cell_id", "@worksheet_id", "@r", "@c", "@ctfa", "@ctt", "@cts", "@bg", "@ff", "@fc", "@fs", "@bl", "@it", "@cl", "@un", "@vt", "@ht", "@tb", "@tr"],
            {
                'cell_id': cell_id,
                'worksheet_id': tid,
                'r': serializer.validated_data['r'],
                'c': serializer.validated_data['c'],
                'ctfa': serializer.validated_data['ctfa'],
                'ctt': serializer.validated_data['ctt'],
                'cts': serializer.validated_data['cts'],
                'bg': serializer.validated_data['bg'],
                'ff': serializer.validated_data['ff'],
                'fc': serializer.validated_data['fc'],
                'fs': serializer.validated_data['fs'],
                'bl': serializer.validated_data['bl'],
                'it': serializer.validated_data['it'],
                'cl': serializer.validated_data['cl'],
                'un': serializer.validated_data['un'],
                'vt': serializer.validated_data['vt'],
                'ht': serializer.validated_data['ht'],
                'tb': serializer.validated_data['tb'],
                'tr': serializer.validated_data['tr'],
            }
        )
        if isinstance(resp, tuple) and resp and resp[0] is False:
            return False, resp[1]
        if resp is False or not getattr(resp, 'txs', None) or getattr(resp, 'ongoingTx', False):
            return False, '写入失败: immudb 未确认事务提交'
        return True, '写入成功'

    def insert_row(self, gid: str, tid: str, cells: list[dict]):
        """Insert an entire new row in one immudb transaction."""
        from common.immudbIO.serializers import WriteDataSerializer

        if not cells:
            return False, '新增行不能为空'
        statements = ['BEGIN TRANSACTION;']
        params = {}
        for index, cell in enumerate(cells):
            serializer = WriteDataSerializer(data=cell)
            if not serializer.is_valid():
                return False, serializer.errors
            values = serializer.validated_data
            coordinate = f"{tid}:{values['r']}:{values['c']}"
            values['cell_id'] = hashlib.sha256(coordinate.encode('utf-8')).hexdigest()
            values['worksheet_id'] = tid
            fields = CELL_SELECT_FIELDS
            placeholders = []
            for field in fields:
                parameter = f'{field}_{index}'
                params[parameter] = values[field]
                placeholders.append(f'@{parameter}')
            statements.append(
                f"INSERT INTO {tid} ({','.join(fields)}) VALUES ({','.join(placeholders)});"
            )
        statements.append('COMMIT;')
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
            response = self.immudb_client.sqlExec('\n'.join(statements), params)
        except Exception as exc:
            return False, f'新增行失败: {exc}'
        if not getattr(response, 'txs', None) or getattr(response, 'ongoingTx', False):
            return False, '新增行失败: immudb 未确认事务提交'
        return True, '写入成功'

    def clear_cells(self, gid: str, tid: str, cells: list[dict[str, int]]):
        """Clear existing cell contents without changing cell formatting or table structure."""
        cleared_cells = []
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
            tclient = toumingClient(self.immudb_client)
            update_query = f"UPDATE {tid} SET ctt=@ctt, cts=@cts WHERE r=@r AND c=@c;"
            for cell in cells:
                row, column = cell['r'], cell['c']
                existing = tclient.simpleSelect(
                    tid,
                    ['cell_id'],
                    {},
                    f"r = {row} and c = {column}",
                )
                if isinstance(existing, tuple) and existing and existing[0] is False:
                    return False, {'message': existing[1], 'cleared_cells': cleared_cells}
                if not existing:
                    continue
                response = self.immudb_client.sqlExec(
                    update_query,
                    {'ctt': CellCttChoices.GENERAL, 'cts': '', 'r': row, 'c': column},
                )
                if not getattr(response, 'txs', None):
                    return False, {'message': '清空单元格失败', 'cleared_cells': cleared_cells}
                cleared_cells.append({'r': row, 'c': column})
            return True, {'cleared_cells': cleared_cells}
        except Exception as exc:
            return False, {'message': f'清空单元格失败: {exc}', 'cleared_cells': cleared_cells}

    def delete_row(self, gid: str, tid: str, row_index: int):
        """Logically remove every cell at a row coordinate from current SQL queries."""
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
            existing = toumingClient(self.immudb_client).simpleSelect(
                tid, ['cell_id'], {}, f'r = {row_index}',
            )
            if isinstance(existing, tuple) and existing and existing[0] is False:
                return False, existing[1]
            if not existing:
                return False, '行不存在或已删除'
            response = self.immudb_client.sqlExec(
                f'DELETE FROM {tid} WHERE r=@r;', {'r': row_index},
            )
            if not getattr(response, 'txs', None):
                return False, '删除行失败'
            return True, len(existing)
        except Exception as exc:
            return False, f'删除行失败: {exc}'

    @staticmethod
    def _utc_now_iso():
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _hash_payload(payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return "0x" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize_cell(row):
        if isinstance(row, dict):
            return {field: row.get(field) for field in CELL_SELECT_FIELDS}
        return {
            field: row[index] if index < len(row) else None
            for index, field in enumerate(CELL_SELECT_FIELDS)
        }

    @staticmethod
    def _build_merkle_tree(leaves):
        if not leaves:
            empty_hash = ImmudbService._hash_payload({"rows": []})
            return empty_hash, [[empty_hash]]

        levels = [leaves]
        current = leaves
        while len(current) > 1:
            next_level = []
            for index in range(0, len(current), 2):
                left = current[index]
                right = current[index + 1] if index + 1 < len(current) else left
                next_level.append(ImmudbService._hash_payload({"left": left, "right": right}))
            levels.append(next_level)
            current = next_level
        return current[0], levels

    @staticmethod
    def _build_merkle_proof(levels, leaf_index):
        proof = []
        index = leaf_index
        for level in levels[:-1]:
            sibling_index = index - 1 if index % 2 else index + 1
            if sibling_index >= len(level):
                sibling_index = index
            proof.append(level[sibling_index])
            index = index // 2
        return proof

    @staticmethod
    def _row_sort_key(row_number):
        try:
            return int(row_number)
        except (TypeError, ValueError):
            return row_number

    @staticmethod
    def _resolve_row_index(row_id, row_entries):
        row_id_text = str(row_id).strip()
        lower_id = row_id_text.lower()

        if lower_id.startswith("row-"):
            suffix = lower_id[4:]
            if suffix.isdigit():
                ordinal = int(suffix)
                if 1 <= ordinal <= len(row_entries):
                    return ordinal - 1

        for index, entry in enumerate(row_entries):
            if str(entry["row_number"]) == row_id_text:
                return index

        if row_id_text.isdigit():
            ordinal = int(row_id_text)
            if 1 <= ordinal <= len(row_entries):
                return ordinal - 1

        for index, entry in enumerate(row_entries):
            if any(str(cell.get("cell_id")) == row_id_text for cell in entry["cells"]):
                return index
        return None

    def _get_table_rows_for_integrity(self, gid: str, tid: str):
        try:
            self.immudb_client.useDatabase(gid.encode('utf-8'))
        except Exception as e:
            return False, f'鏁版嵁搴撲笉瀛樺湪: {str(e)}'

        tclient = toumingClient(self.immudb_client)
        raw_result = tclient.simpleSelect(tid, CELL_SELECT_FIELDS, {}, "r >= 0")
        if raw_result is False or isinstance(raw_result, tuple):
            return False, raw_result

        grouped = {}
        for raw_row in raw_result or []:
            cell = self._normalize_cell(raw_row)
            row_number = cell.get("r")
            grouped.setdefault(row_number, []).append(cell)

        row_entries = []
        for row_number in sorted(grouped.keys(), key=self._row_sort_key):
            cells = sorted(grouped[row_number], key=lambda cell: self._row_sort_key(cell.get("c")))
            row_hash = self._hash_payload({"row": row_number, "cells": cells})
            row_entries.append({
                "row_number": row_number,
                "cells": cells,
                "hash": row_hash,
            })
        return True, row_entries

    def get_table_integrity(self, gid: str, tid: str):
        success, rows_or_error = self._get_table_rows_for_integrity(gid, tid)
        if not success:
            return False, rows_or_error

        row_entries = rows_or_error
        leaves = [entry["hash"] for entry in row_entries]
        merkle_root, _levels = self._build_merkle_tree(leaves)
        verified_rows = len(row_entries)
        return True, {
            "total_rows": len(row_entries),
            "verified_rows": verified_rows,
            "merkle_root": merkle_root,
            "integrity_score": 100 if verified_rows == len(row_entries) else 0,
            "last_verification_time": self._utc_now_iso(),
        }

    def get_row_integrity(self, gid: str, tid: str, row_id: str):
        success, rows_or_error = self._get_table_rows_for_integrity(gid, tid)
        if not success:
            return False, rows_or_error

        row_entries = rows_or_error
        row_index = self._resolve_row_index(row_id, row_entries)
        if row_index is None:
            return False, 'row data not found'

        leaves = [entry["hash"] for entry in row_entries]
        _merkle_root, levels = self._build_merkle_tree(leaves)
        row_entry = row_entries[row_index]
        now = self._utc_now_iso()
        return True, {
            "row_id": str(row_id),
            "hash": row_entry["hash"],
            "block_height": row_index + 1,
            "timestamp": now,
            "merkle_proof": self._build_merkle_proof(levels, row_index),
            "verified": True,
            "verification_time": now,
        }
