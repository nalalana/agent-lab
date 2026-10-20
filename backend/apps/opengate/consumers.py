"""
/backend/apps/opengate/consumers.py
WebSocket consumer that publishes real-time edits per table.
"""
import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any, Dict, Optional

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import get_or_create_principal, is_granted
from apps.opengate.models import GatesTable
from apps.opengate.services import OpenGateService
from apps.user.models import User

logger = logging.getLogger("touming")

TABLE_EDITING_STATE: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
TABLE_LOCKS: Dict[str, asyncio.Lock] = {}


def _lock_for(table_id: str) -> asyncio.Lock:
    lock = TABLE_LOCKS.get(table_id)
    if not lock:
        lock = asyncio.Lock()
        TABLE_LOCKS[table_id] = lock
    return lock


def _cell_key(row_index: int, column_index: int) -> str:
    return f"{row_index}:{column_index}"


class TableEditingConsumer(AsyncJsonWebsocketConsumer):
    """Broadcasts which cells are being edited inside a table."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_cell_key: Optional[str] = None
        self.can_write = False

    async def connect(self):
        self.user_id = self.scope.get("user_id")
        self.table_id = self.scope["url_route"]["kwargs"].get("table_id")
        self.session_id = str(uuid.uuid4())
        if not self.user_id or not self.table_id:
            logger.warning(f"[table ws] missing identifiers scope={self.scope}")
            await self.close(code=4001)
            return

        try:
            self.user = await database_sync_to_async(User.objects.get)(id=self.user_id)
            self.table = await database_sync_to_async(GatesTable.objects.get)(tid=self.table_id)
        except Exception:
            logger.exception(f"[table ws] load user/table failed user_id={self.user_id} table_id={self.table_id}")
            await self.close(code=4002)
            return

        ok, _ = await database_sync_to_async(OpenGateService.get_table_full_details)(self.user, self.table_id)
        if not ok:
            logger.warning(f"[table ws] table details denied user={self.user_id} table={self.table_id}")
            await self.close(code=4003)
            return

        self.group_name = f"table_editing_{self.table_id}"
        self.active_cell_key: Optional[str] = None
        self.can_write = await self._has_write_permission()

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._send_state_snapshot()

    async def disconnect(self, close_code):
        if self.active_cell_key:
            await self._remove_cell(self.active_cell_key)
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content: Dict[str, Any], **_kwargs):
        message_type = content.get("type")
        if message_type == "start_edit":
            await self._handle_start(content.get("cell") or {})
        elif message_type == "stop_edit":
            await self._handle_stop(content.get("cell") or {})
        elif message_type == "ping":
            await self.send_json({"type": "pong", "timestamp": content.get("timestamp")})

    async def _handle_start(self, payload: Dict[str, Any]):
        if not self.can_write:
            return
        row = payload.get("rowIndex")
        col = payload.get("columnIndex")
        if row is None or col is None:
            return
        cell = {
            "rowIndex": row,
            "columnIndex": col,
            "columnKey": payload.get("columnKey") or f"col_{col}",
            "userId": str(self.user_id),
            "userName": self.user.username or self.user.user_name or "无名",
        }
        cell_key = _cell_key(row, col)
        if self.active_cell_key and self.active_cell_key != cell_key:
            await self._remove_cell(self.active_cell_key)
        self.active_cell_key = cell_key
        await self._add_cell(cell_key, cell)
        await self._broadcast_cell("start", cell)

    async def _handle_stop(self, payload: Dict[str, Any]):
        row = payload.get("rowIndex")
        col = payload.get("columnIndex")
        cell_key = self.active_cell_key or (_cell_key(row, col) if row is not None and col is not None else None)
        if not cell_key:
            return
        await self._remove_cell(cell_key)
        if self.active_cell_key == cell_key:
            self.active_cell_key = None

    async def _add_cell(self, cell_key: str, payload: Dict[str, Any]):
        lock = _lock_for(self.table_id)
        async with lock:
            TABLE_EDITING_STATE[self.table_id][cell_key] = payload

    async def _remove_cell(self, cell_key: str):
        lock = _lock_for(self.table_id)
        removed = None
        async with lock:
            namespace = TABLE_EDITING_STATE.get(self.table_id)
            if namespace:
                removed = namespace.pop(cell_key, None)
                if not namespace:
                    TABLE_EDITING_STATE.pop(self.table_id, None)
        if removed:
            await self._broadcast_cell("stop", removed)

    async def _broadcast_cell(self, action: str, cell: Dict[str, Any]):
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "table_editing_event",
                "action": action,
                "cell": cell,
            },
        )

    async def table_editing_event(self, event: Dict[str, Any]):
        await self.send_json(
            {
                "type": "editing_cell",
                "action": event.get("action"),
                "cell": event.get("cell"),
            }
        )

    async def _send_state_snapshot(self):
        namespace = TABLE_EDITING_STATE.get(self.table_id, {})
        await self.send_json(
            {
                "type": "editing_state",
                "cells": list(namespace.values()),
            }
        )

    @database_sync_to_async
    def _has_write_permission(self) -> bool:
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=self.user.uid)
        resource = Resource.objects.filter(
            type=Resource.TYPE_TABLE,
            external_id=self.table.tid,
        ).first()
        return bool(
            resource
            and is_granted(principal=principal, resource=resource, permissions=Grant.PERM_WRITE)
        )
