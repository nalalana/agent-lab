"""
/backend/apps/opengate/services.py
OpenGate服务类
"""
import time
import json
import logging
from typing import Any, Optional

from django.db import transaction
from django.db.models import Max, Q

from apps.opengate.models import Gates, APIKey, TableAuditEvent
from apps.user.models import User
from apps.iam.models import Grant, Principal, Resource
from apps.iam.services import (
    get_or_create_personal_tenant,
    get_or_create_organization_tenant,
    get_or_create_principal,
    get_or_create_resource,
    grant_permissions,
    is_granted,
    revoke_permissions,
    visible_resource_ids,
)
from common.utils.Tengcentcos_utils import get_presigned_url, put_object, normalize_cos_key
from apps.moderation.models import TextModerationAsset
from apps.moderation.services import ImageModerationService, TextModerationService

from properties import db_local_base_url, db_local_base_port, USER, PASSWORD
from apps.opengate.models import GatesTable
from common.immudbIO.services import ImmudbService
from common.immudbIO.errors import UnconfirmedWriteError
from common.immudbIO.models import CellCtfaChoices, CellCttChoices
from common.immudbIO.serializers import WriteDataSerializer


logger = logging.getLogger('touming')


class TableWriteConflict(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


class TableCellValidationError(ValueError):
    """All cell payloads were checked before any remote mutation was attempted."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"单元格写入参数无效: {errors}")


TABLE_WRITE_FENCE_ACTION = 'write.fence'


class TableWriteDurabilityError(RuntimeError):
    code = 'TABLE_WRITE_DURABILITY_REQUIRED'


def _require_table_autocommit():
    # A nested atomic block cannot durably record intent before the remote call.
    if transaction.get_connection().in_atomic_block or not transaction.get_autocommit():
        raise TableWriteDurabilityError('受管表格操作需要独立事务边界，本次未派发。')


def _assert_table_write_fence(table, expected_fence_id=None):
    pending = list(TableAuditEvent.objects.filter(
        table=table, action=TABLE_WRITE_FENCE_ACTION, result=TableAuditEvent.RESULT_PENDING,
    ).order_by('id').values_list('id', flat=True)[:2])
    if pending != ([expected_fence_id] if expected_fence_id is not None else []):
        raise UnconfirmedWriteError()


def _cell_versions(table, cells):
    coordinates = {(cell['r'], cell['c']) for cell in cells}
    versions = {coordinate: 0 for coordinate in coordinates}
    if not coordinates:
        return versions
    events = TableAuditEvent.objects.filter(
        table=table,
        action__in=('cell.upsert', 'data.upsert', 'cell.clear'),
        result=TableAuditEvent.RESULT_SUCCESS,
        row_index__in={row for row, _ in coordinates},
        column_index__in={column for _, column in coordinates},
    ).order_by().values('row_index', 'column_index').annotate(version=Max('id'))
    for event in events:
        coordinate = (event['row_index'], event['column_index'])
        if coordinate in versions:
            versions[coordinate] = event['version']
    # Row deletion invalidates even coordinates absent from the latest read.
    deleted_rows = {
        event['row_index']: event['version']
        for event in TableAuditEvent.objects.filter(
            table=table, action='row.delete', result=TableAuditEvent.RESULT_SUCCESS,
            row_index__in={row for row, _ in coordinates},
        ).order_by().values('row_index').annotate(version=Max('id'))
    }
    for coordinate in versions:
        versions[coordinate] = max(versions[coordinate], deleted_rows.get(coordinate[0], 0))
    return versions


def _table_version(table):
    return TableAuditEvent.objects.filter(
        table=table, result=TableAuditEvent.RESULT_SUCCESS,
    ).exclude(action=TABLE_WRITE_FENCE_ACTION).order_by('-id').values_list('id', flat=True).first() or 0


def _ip_suffix(client_ip: Optional[str]) -> str:
    return f" ip={client_ip}" if client_ip else ""


def _start_table_audit(table, actor, action, client_ip=None, api_key=None, **fields):
    return TableAuditEvent.objects.create(
        table=table,
        actor=actor,
        api_key=api_key,
        api_key_name=api_key.key_name if api_key else '',
        source=TableAuditEvent.SOURCE_API_KEY if api_key else TableAuditEvent.SOURCE_WEB,
        action=action,
        ip_address=client_ip or None,
        **fields,
    )


def _finish_table_audit(event, success):
    event.result = TableAuditEvent.RESULT_SUCCESS if success else TableAuditEvent.RESULT_FAILED
    event.save(update_fields=['result'])


def _moderate_opengate_texts(
    *,
    items: list[tuple[str, Any]],
    source: str,
    owner_type: str = "project",
    owner_id: str = "",
    actor_id: str = "",
) -> tuple[bool, str]:
    payload = [
        {
            "text": value,
            "source": source,
            "source_field": field,
            "owner_type": owner_type if owner_id else "",
            "owner_id": owner_id,
            "actor_type": "user" if actor_id else "",
            "actor_id": actor_id,
        }
        for field, value in items
        if value is not None
    ]
    ok, message, _asset = TextModerationService.check_texts(payload)
    return ok, message


def _table_write_text_items(values: Any) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(values, dict):
        for key, value in values.items():
            if key in {"r", "c", "cell_id", "ctfa", "ctt"}:
                continue
            if isinstance(value, (dict, list, tuple)):
                items.extend((f"{key}.{field}", text) for field, text in _table_write_text_items(value))
            elif isinstance(value, str):
                items.append((str(key), value))
    elif isinstance(values, list):
        for index, value in enumerate(values):
            if isinstance(value, dict):
                if "value" in value and isinstance(value.get("value"), str):
                    items.append((f"cells[{index}].value", value.get("value")))
                elif "cts" in value and isinstance(value.get("cts"), str):
                    items.append((f"cells[{index}].cts", value.get("cts")))
                else:
                    items.extend((f"cells[{index}].{field}", text) for field, text in _table_write_text_items(value))
            elif isinstance(value, (list, tuple)) and len(value) > 2 and isinstance(value[2], str):
                items.append((f"cells[{index}].value", value[2]))
            elif isinstance(value, str):
                items.append((f"cells[{index}]", value))
    return items


def _timestamp_to_iso(value: Any) -> str | None:
    if value in [None, ""]:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return str(value)


def _generate_column_name(index: int) -> str:
    """
    生成列名：A, B, C, ..., Z, AA, BB, CC, ..., ZZ, AAA, BBB, ...
    索引从0开始：
    - 0-25: A-Z (单字母)
    - 26-51: AA-ZZ (双字母，重复相同字母)
    - 52-77: AAA-ZZZ (三字母，重复相同字母)
    以此类推
    """
    if index < 26:
        # A-Z
        return chr(65 + index)  # 65 is ASCII for 'A'
    else:
        # AA, BB, CC, ... (重复相同字母)
        letter_index = (index - 26) % 26
        letter = chr(65 + letter_index)
        num_repeats = (index - 26) // 26 + 2  # 从2个字母开始（AA）
        return letter * num_repeats


class OpenGateService:
    @staticmethod
    def is_valid_sql_type(sql_type: str) -> bool:
        valid = ['INTEGER', 'BOOLEAN', 'VARCHAR', 'FILE', 'TIMESTAMP', 'FLOAT']
        return (sql_type or '').upper() in valid

    @staticmethod
    def _has_iam_permission(
        user: User | None,
        resource_type: str,
        external_id: str,
        permission: int,
    ) -> bool:
        if user is None:
            return False
        principal = Principal.objects.filter(
            type=Principal.TYPE_USER,
            external_id=user.uid,
        ).first()
        resource = Resource.objects.filter(
            type=resource_type,
            external_id=external_id,
        ).first()
        return bool(
            principal
            and resource
            and is_granted(principal=principal, resource=resource, permissions=permission)
        )

    @staticmethod
    def can_read_project(user: User | None, gate: Gates) -> bool:
        return gate.allows_public_read() or OpenGateService.can_read_project_internally(user, gate)

    @staticmethod
    def can_read_project_internally(user: User | None, gate: Gates) -> bool:
        return OpenGateService._has_iam_permission(
            user,
            Resource.TYPE_PROJECT,
            gate.gid,
            Grant.PERM_READ,
        )

    @staticmethod
    def can_manage_project(user: User | None, gate: Gates) -> bool:
        return OpenGateService._has_iam_permission(
            user,
            Resource.TYPE_PROJECT,
            gate.gid,
            Grant.PERM_ADMIN,
        )

    @staticmethod
    def can_read_table(user: User | None, table: GatesTable) -> bool:
        return table.gate.allows_public_read() or OpenGateService._has_iam_permission(
            user,
            Resource.TYPE_TABLE,
            table.tid,
            Grant.PERM_READ,
        )

    @staticmethod
    def can_write_table(user: User | None, table: GatesTable) -> bool:
        # Public writes still require an authenticated account for auditability.
        if user is not None and table.gate.allows_public_write():
            return True
        return OpenGateService._has_iam_permission(
            user,
            Resource.TYPE_TABLE,
            table.tid,
            Grant.PERM_WRITE,
        )

    @staticmethod
    def table_public_permission_limit(gate: Gates) -> int:
        return {
            Gates.PUBLIC_ACCESS_INTERNAL: GatesTable.PERM_PRIVATE,
            Gates.PUBLIC_ACCESS_READ: GatesTable.PERM_READ,
            Gates.PUBLIC_ACCESS_WRITE: GatesTable.PERM_WRITE,
        }.get(gate.public_access, GatesTable.PERM_PRIVATE)

    @staticmethod
    @transaction.atomic
    def update_public_access(user: User, gate: Gates, public_access: str):
        if public_access not in dict(Gates.PUBLIC_ACCESS_CHOICES):
            return False, '公开范围设置无效'
        if not OpenGateService.can_manage_project(user, gate):
            return False, '用户无权限修改项目公开范围'
        gate = Gates.objects.select_for_update().get(pk=gate.pk)
        if gate.public_access != public_access:
            gate.public_access = public_access
            gate.updated_at = str(time.time())[:20]
            gate.save(update_fields=['public_access', 'updated_at'])
        # Tightening a project also clamps historical child-table settings.
        limit = OpenGateService.table_public_permission_limit(gate)
        GatesTable.objects.filter(gate=gate, permission__gt=limit).update(permission=limit)
        return True, gate.public_access
        
    @staticmethod
    def create_gate(owner: User, gid: str, gatename: str,logo: str = "", description: str = "", background_image: str = "", client_ip: Optional[str] = None, company_id: Optional[str] = None):
        company = None
        if company_id:
            from apps.company.models import Company
            from apps.company.permissions import can_access_formal_company

            company = Company.objects.filter(cid=company_id).first()
            if company is None or not can_access_formal_company(company, owner, Grant.PERM_SUPER_ADMIN):
                return None, '公司不存在或无权限创建项目'
        count = Gates.objects.filter(owner=owner).count()
        if count >= 500:
            logger.error(f"/api/opengate/gates failed:{_ip_suffix(client_ip)} err=您创建的应用数量已达上限，请联系管理员")
            return None, '您创建的应用数量已达上限，请联系管理员'
        moderation_ok, moderation_msg = _moderate_opengate_texts(
            items=[("gatename", gatename), ("description", description)],
            source=TextModerationAsset.SOURCE_OPENGATE_PROJECT,
            owner_id=gid,
            actor_id=getattr(owner, "uid", ""),
        )
        if not moderation_ok:
            return None, moderation_msg
        try:
            immudb_service = ImmudbService()
            success, message = immudb_service.create_db(gid)
            if not success:
                logger.error(f"create_gate failed:{_ip_suffix(client_ip)} err={message}")
                return None, message

            logo_ok, logo_key, logo_msg = ImageModerationService.require_approved_public_image(logo)
            if not logo_ok:
                return None, logo_msg
            bg_ok, background_key, bg_msg = ImageModerationService.require_approved_public_image(background_image)
            if not bg_ok:
                return None, bg_msg

            with transaction.atomic():
                if company:
                    from apps.company.models import Company, companyGate
                    from apps.company.permissions import can_access_formal_company

                    company = Company.objects.select_for_update().get(pk=company.pk)
                    if not can_access_formal_company(company, owner, Grant.PERM_SUPER_ADMIN):
                        return None, '权限不足'
                gate = Gates.objects.create(
                    gid=gid,
                    gatename=gatename,
                    logo=logo_key,
                    description=description or "",
                    background_image=background_key,
                    owner=owner,
                    label=Gates.LABEL_COMPANY if company else Gates.LABEL_PERSONAL,
                    created_at=str(time.time())[:20],
                    updated_at=str(time.time())[:20],
                )
                if company:
                    companyGate.objects.create(company=company, opengate=gate)
                principal = get_or_create_principal(Principal.TYPE_USER, external_id=owner.uid)
                resource = get_or_create_resource(
                    Resource.TYPE_PROJECT,
                    external_id=gate.gid,
                    tenant=get_or_create_organization_tenant(company) if company else get_or_create_personal_tenant(owner),
                    creator=owner,
                )
                grant_permissions(principal=principal, resource=resource, permissions=Grant.PERM_SUPER_ADMIN)
            return gate, '创建成功'
        except Exception as e:
            logger.error(f"create_gate failed:{_ip_suffix(client_ip)} err={e}")
            return None, f'创建失败: {str(e)}'

    @staticmethod
    def delete_gate(user: User, gate: Gates, client_ip: Optional[str] = None):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = Resource.objects.filter(
            type=Resource.TYPE_PROJECT,
            external_id=gate.gid,
        ).first()
        if resource is None or not is_granted(
            principal=principal,
            resource=resource,
            permissions=Grant.PERM_SUPER_ADMIN,
        ):
            logger.error(
                f"delete_gate denied: user={user.username} gid={gate.gid}"
                f"{_ip_suffix(client_ip)}"
            )
            return False, '用户无权限删除该项目'

        immutable_ok, immutable_message = ImmudbService().delete_db(gate.gid)
        if not immutable_ok:
            logger.error(
                f"delete_gate immudb failed: gid={gate.gid}{_ip_suffix(client_ip)} "
                f"err={immutable_message}"
            )
            return False, immutable_message

        try:
            with transaction.atomic():
                gate.delete()
                resource.delete()
        except Exception as e:
            logger.exception(
                f"delete_gate relational cleanup failed: gid={gate.gid}"
                f"{_ip_suffix(client_ip)} err={e}"
            )
            return False, f'删除失败: {str(e)}'

        return True, '删除成功'

    @staticmethod
    @transaction.atomic
    def create_table_full(owner: User, gid: str,  table_name: str, description: str, permission: int, column_titles: list[str], num_columns: int, client_ip: Optional[str] = None):
        from apps.opengate.models import GatesTable
        import uuid
        
        try:
            gate = Gates.objects.select_for_update().get(gid=gid)
        except Exception as e:
            logger.error(f"create_table_full gate missing:{_ip_suffix(client_ip)} err={e}")
            return False, '项目不存在'
        
        if GatesTable.objects.filter(gate=gate, name=table_name).exists():
            return False, '表格已存在'
        
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=owner.uid)
        project_resource = get_or_create_resource(Resource.TYPE_PROJECT, external_id=gid)
        if not is_granted(principal=principal, resource=project_resource, permissions=Grant.PERM_ADMIN):
            return False, '用户无权限创建表格'
        
        # 验证权限值：0=private, 1=read, 2=write
        from apps.opengate.models import GatesTable
        if permission not in [GatesTable.PERM_PRIVATE, GatesTable.PERM_READ, GatesTable.PERM_WRITE]:
            return False, '权限设置错误，请使用 0(private)、1(read) 或 2(write)'
        
        if permission > OpenGateService.table_public_permission_limit(gate):
            return False, '表格公开范围不能超过所属项目'

        # 验证列数
        if num_columns != len(column_titles):
            return False, '列数不匹配'
        
        # 生成immudb列名：A, B, C, D...AA, BB, CC, DD...

        moderation_ok, moderation_msg = _moderate_opengate_texts(
            items=[
                ("tableName", table_name),
                ("description", description),
                *[(f"columnTitles[{index}]", title) for index, title in enumerate(column_titles or [])],
            ],
            source=TextModerationAsset.SOURCE_OPENGATE_TABLE,
            owner_type="project",
            owner_id=gid,
            actor_id=getattr(owner, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg

        columns_config = []
        
        for i in range(num_columns):
            column_name = _generate_column_name(i)
            # immudb中所有列都是VARCHAR类型（字符串）
            
            # 存储映射：用户自定义标题 -> immudb列名
            columns_config.append({
                'field_name': column_name,  # immudb中的列名（A, B, C, D...）
                'title': column_titles[i] if i < len(column_titles) else f'Column {i+1}',  # 用户自定义标题
                'column_index': i,
                'data_type': 'string'  # 统一为字符串类型
            })
        
        try:
            # 生成tid（表格唯一标识）
            tid = "table"+str(uuid.uuid4()).replace("-", "")
            immudb_service = ImmudbService()
            success, message = immudb_service.create_table(gid, tid, num_columns)
            if not success:
                logger.error(f"create_table_full failed:{_ip_suffix(client_ip)} err={message}")
                return False, message
            
            print(tid, gate, table_name, permission, num_columns, columns_config)
            GatesTable.objects.create(
                tid=tid,
                description=description or '',
                gate=gate,
                name=table_name,
                permission=permission,
                num_columns=num_columns,
                columns_config=columns_config,
                created_at=str(time.time())[:20],
                updated_at=str(time.time())[:20]
            )
            principal = get_or_create_principal(Principal.TYPE_USER, external_id=owner.uid)
            table_resource = get_or_create_resource(
                Resource.TYPE_TABLE,
                external_id=tid,
                parent=project_resource,
                tenant=project_resource.tenant,
                creator=owner,
            )
            grant_permissions(principal=principal, resource=table_resource, permissions=Grant.PERM_SUPER_ADMIN)
            return True, '创建成功！'
        except Exception as e:
            logger.error(f"create_table_full failed:{_ip_suffix(client_ip)} err={e}")
            return False, f'创建失败: {str(e)}'

    @staticmethod
    def list_gate_keys(user: User, gate: Gates):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_PROJECT, external_id=gate.gid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN):
            return []
        keys = APIKey.objects.filter(gate=gate)
        return [key.get_info() for key in keys]

    @staticmethod
    def list_gates_by_owner(uid: str, limit: int = 20, offset: int = 0):
        user = User.objects.get(uid=uid)
        resource_ids = visible_resource_ids(user, Resource.TYPE_PROJECT)
        gates = Gates.objects.filter(gid__in=resource_ids)
        total = gates.count()
        gates = list(gates.order_by('id')[offset: offset + limit])
        from apps.company.models import Company

        gids = [gate.gid for gate in gates]
        tenant_by_gid = dict(Resource.objects.filter(
            type=Resource.TYPE_PROJECT,
            external_id__in=gids,
            tenant__type='organization',
        ).values_list('external_id', 'tenant__external_id'))
        cid_by_gid = {
            gid: tenant_id.removeprefix('organization:')
            for gid, tenant_id in tenant_by_gid.items()
            if tenant_id.startswith('organization:')
        }
        company_names = dict(Company.objects.filter(
            cid__in=cid_by_gid.values(),
        ).values_list('cid', 'company_name'))
        gates_data = []
        for gate in gates:
            info = gate.get_info()
            cid = cid_by_gid.get(gate.gid)
            info['cid'] = cid
            info['company_name'] = company_names.get(cid) if cid else None
            gates_data.append(info)
        has_more = (offset + limit) < total
        return {'total': total, 'gates': gates_data, 'hasMore': has_more}

    @staticmethod
    def list_tables(user: User | None, gid: str, limit: int, offset: int):
        from apps.opengate.models import GatesTable
        try:
            gate = Gates.objects.get(gid=gid)
        except Exception:
            return False, '应用不存在'
        if not OpenGateService.can_read_project(user, gate):
            return False, '无权访问或资源不存在'
        if gate.allows_public_read():
            tables = GatesTable.objects.filter(gate=gate)
        else:
            project_resource = Resource.objects.filter(
                type=Resource.TYPE_PROJECT,
                external_id=gid,
            ).first()
            readable_ids = visible_resource_ids(user, Resource.TYPE_TABLE, project_resource.tenant)
            tables = GatesTable.objects.filter(gate=gate, tid__in=readable_ids)
        total = tables.count()
        tables = list(tables.order_by('id')[offset: offset + limit])
        # 将 GatesTable 对象转换为可序列化的字典
        tables_data = [table.get_info() for table in tables]
        has_more = (offset + limit) < total
        return True, {'total': total, 'tables': tables_data, 'hasMore': has_more}      


    @staticmethod
    def get_table_full_details(user: User | None, tid: str):
        """
        返回表的基础信息与结构信息，包含权限校验。
        成功: (True, { 'table_info': {...}, 'table_structure': str })
        失败: (False, '错误信息')
        """
        try:
            table = GatesTable.objects.get(tid=tid)
        except Exception:
            return False, '应用或表格不存在'

        if not OpenGateService.can_read_table(user, table):
            return False, '无权访问或资源不存在'

        table_info = {
            'tid': table.tid,
            'gid': table.gate.gid,
            'project_name': table.gate.gatename,
            'name': table.name,
            'permission': table.permission,
            'public_access': table.gate.public_access,
            'max_public_permission': OpenGateService.table_public_permission_limit(table.gate),
            'can_edit_schema': OpenGateService._has_iam_permission(user, Resource.TYPE_TABLE, table.tid, Grant.ROLE_EDITOR),
            'description': table.description or '',
            'columns_config': table.columns_config,
            'can_write': OpenGateService.can_write_table(user, table),
        }

        return True, table_info

    @staticmethod
    def update_gate(user: User, gate: Gates, gatename: str, logo: str | None,  description: str | None, background_image: str | None, client_ip: Optional[str] = None):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_PROJECT, external_id=gate.gid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN):
            return False, '用户无权限更新该应用'
        try:
            moderation_ok, moderation_msg = _moderate_opengate_texts(
                items=[("gatename", gatename), ("description", description)],
                source=TextModerationAsset.SOURCE_OPENGATE_PROJECT,
                owner_id=gate.gid,
                actor_id=getattr(user, "uid", ""),
            )
            if not moderation_ok:
                return False, moderation_msg
            if gatename is not None and gatename.strip() != '':
                gate.gatename = gatename
            if description is not None and description.strip() != '':
                gate.description = description
            if background_image is not None and background_image.strip() != '':
                bg_ok, background_key, bg_msg = ImageModerationService.require_approved_public_image(background_image)
                if not bg_ok:
                    return False, bg_msg
                gate.background_image = background_key
            if logo is not None and logo.strip() != '':
                logo_ok, logo_key, logo_msg = ImageModerationService.require_approved_public_image(logo)
                if not logo_ok:
                    return False, logo_msg
                gate.logo = logo_key
            gate.updated_at = str(time.time())[:20]
            gate.save()
            return True, '更新成功'
        except Exception as e:
            logger.error(f"update_gate failed:{_ip_suffix(client_ip)} err={e}")
            return False, '更新失败'

    @staticmethod
    @transaction.atomic
    def update_table_full(
        owner: User, 
        tid: str, 
        table_name: str, 
        description: Optional[str] = None,
        permission: Optional[int] = None,
        column_titles: Optional[list[str]] = None,
        num_columns: Optional[int] = None,
        client_ip: Optional[str] = None
    ):
        """
        更新表格信息
        支持更新：描述、权限、列标题和列数
        """
        from apps.opengate.models import GatesTable
        
        try:
            table = GatesTable.objects.get(tid=tid)
        except Exception as e:
            logger.error(f"update_table_full gate missing:{_ip_suffix(client_ip)} err={e}")
            return False, '应用不存在或者用户无权限'
        
        # 检查权限：只有管理员或拥有者才能更新
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=owner.uid)
        resource = get_or_create_resource(Resource.TYPE_TABLE, external_id=tid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN):
            return False, '用户无权限管理该表'

        before = {
            'name': table.name,
            'description': table.description or '',
            'permission': table.permission,
            'columns': table.get_column_titles(),
        }

        # Serialize project/table visibility mutations against the same parent.
        table.gate = Gates.objects.select_for_update().get(pk=table.gate_id)
        # Validate before changing any stored metadata.
        if permission is not None:
            if permission not in [GatesTable.PERM_PRIVATE, GatesTable.PERM_READ, GatesTable.PERM_WRITE]:
                return False, '权限设置错误，请使用 0(private)、1(read) 或 2(write)'
            if permission > OpenGateService.table_public_permission_limit(table.gate):
                return False, '表格公开范围不能超过所属项目'
        if num_columns is not None and (not isinstance(num_columns, int) or not 1 <= num_columns <= 26):
            return False, '列数必须在1至26之间'

        # 更新表格名称
        moderation_ok, moderation_msg = _moderate_opengate_texts(
            items=[
                ("tableName", table_name),
                ("description", description),
                *[(f"columnTitles[{index}]", title) for index, title in enumerate(column_titles or [])],
            ],
            source=TextModerationAsset.SOURCE_OPENGATE_TABLE,
            owner_type="table",
            owner_id=tid,
            actor_id=getattr(owner, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg

        if table_name is not None:
            table.name = table_name
        
        # 更新描述
        if description is not None:
            table.description = description
        
        # 更新权限
        if permission is not None:
            if permission not in [GatesTable.PERM_PRIVATE, GatesTable.PERM_READ, GatesTable.PERM_WRITE]:
                return False, '权限设置错误，请使用 0(private)、1(read) 或 2(write)'
            table.permission = permission
        
        # 更新列标题和列数
        if column_titles is not None and num_columns is not None:
            if num_columns != len(column_titles):
                return False, '列数不匹配'
            
            # 更新 columns_config
            columns_config = []
            for i in range(num_columns):
                column_name = _generate_column_name(i)
                columns_config.append({
                    'field_name': column_name,
                    'title': column_titles[i] if i < len(column_titles) else f'Column {i+1}',
                    'column_index': i,
                    'data_type': 'string'
                })
            
            table.columns_config = columns_config
            table.num_columns = num_columns
        elif column_titles is not None or num_columns is not None:
            # 如果只提供了其中一个，则返回错误
            return False, '列标题和列数必须同时提供'
        
        # Stored cells use coordinates; shrinking the visible structure must
        # never physically delete immutable historical cell records.
        table.permission = min(table.permission, OpenGateService.table_public_permission_limit(table.gate))
        table.updated_at = str(time.time())[:20]
        table.save()
        after = {
            'name': table.name,
            'description': table.description or '',
            'permission': table.permission,
            'columns': table.get_column_titles(),
        }
        before_schema = {key: value for key, value in before.items() if key != 'name'}
        after_schema = {key: value for key, value in after.items() if key != 'name'}

        if before['name'] != after['name']:
            event = _start_table_audit(
                table, owner, 'table.rename', client_ip,
                details={
                    'before': {'name': before['name']},
                    'after': {'name': after['name']},
                },
            )
            _finish_table_audit(event, True)

        if before_schema != after_schema:
            event = _start_table_audit(
                table, owner, 'schema.update', client_ip,
                details={'before': before_schema, 'after': after_schema},
            )
            _finish_table_audit(event, True)

        return True, '更新成功'

    @staticmethod
    def search_gates(user: User, keyword: str, limit: int, offset: int):
        qs = Gates.objects.filter(gid__in=visible_resource_ids(user, Resource.TYPE_PROJECT))
        if keyword:
            qs = qs.filter(Q(gatename__icontains=keyword) | Q(description__icontains=keyword))
        total = qs.count()
        items = list(qs.order_by('id')[offset: offset + limit])
        
        # 计算是否有更多数据
        has_more = (offset + limit) < total
        
        result = []
        for gate in items:
            result.append(gate.get_info())
        return {'total': total, 'items': result, 'hasMore': has_more}

    @staticmethod
    def list_table_members(user: User, tid: str, limit: int, offset: int):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_TABLE, external_id=tid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.ROLE_MANAGER):
            return False, '用户无权限管理该表格'
        table = GatesTable.objects.get(tid=tid)

        # 读取该表的授权，构建 principal_external_id -> permissions 的映射
        grants = Grant.objects.filter(resource=resource)
        principal_to_perm = {g.principal.external_id: g.permissions for g in grants}

        # 找到对应的用户并分页
        principals = list(principal_to_perm.keys())
        qs = User.objects.filter(uid__in=principals).order_by('id')
        total = qs.count()
        users = list(qs[offset: offset + limit])

        # 组装返回结构：手机号，uid，username，权限等级，表格名称
        items = []
        for u in users:
            items.append({
                'phone': u.phone,
                'uid': u.uid,
                'username': u.username,
                'permission': principal_to_perm.get(u.uid, 0),
                'table_name': table.name,
            })

        has_more = (offset + limit) < total
        return True, {'total': total, 'items': items, 'hasMore': has_more}


    @staticmethod
    def transfer_owner(gatename: str, new_owner_username: str, client_ip: Optional[str] = None):
        try:
            gate = Gates.objects.get(gatename=gatename)
            new_owner = User.objects.get(username=new_owner_username, is_activate=1)
            gate.owner = new_owner
            gate.updated_at = str(time.time())[:20]
            gate.save()
            return True
        except Exception as e:
            logger.error(f"transfer_owner failed:{_ip_suffix(client_ip)} err={e}")
            return False

    @staticmethod
    def app_table_details(gatename: str, table_name: str):
        from apps.opengate.models import GatesTable
        gate = Gates.objects.get(gatename=gatename)
        table = GatesTable.objects.get(gate=gate, name=table_name)
        table_info = {
            'table_id': table.id,
            'gate': table.gate.gatename,
            'table_name': table.name,
            'permission': table.permission,
            'updated_at': table.updated_at,
            'description': table.description,
        }
        return gate, table, table_info

    @staticmethod
    def app_table_data(gatename: str, table_name: str, what_to_select: list[str] | None, params: dict | None, limit: int, offset: int):
        # Reuse select logic; params/conditions can be handled by client wrapper
        conditions = [f"id > {offset * limit} and id < {offset * limit + limit + 1}"]
        rows = OpenGateService.select_table_data(gatename, table_name, what_to_select or ['*'], conditions)
        
        # 获取总数用于计算hasMore
        from common.immudbIO.services import ImmudbService
        immudb_service = ImmudbService()
        total = immudb_service.get_table_count(gatename, table_name)
        
        # 计算是否有更多数据
        has_more = (offset + limit) < total
        
        return {
            'rows': rows,
            'total': total,
            'hasMore': has_more
        }

    @staticmethod
    def app_upsert_table_data(gatename: str, table_name: str, fields: list[str], values: str, params: dict, client_ip: Optional[str] = None):
        return OpenGateService.upsert_table_data(gatename, table_name, fields, values, params, client_ip)

    @staticmethod
    def check_invitations(user: User, limit: int, offset: int):
        from apps.opengate.models import GatesMember
        gms = GatesMember.objects.filter(member=user, is_valid=0).order_by('id')[offset: offset+limit]
        invitation_info = []
        for gm in gms:
            invitation_info.append({
                'invitation_id': gm.id,
                'gate_name': gm.table.gate.gatename,
                'table_name': gm.table.name,
                'permission': gm.permission,
                'updated_at': gm.updated_at,
            })
        return invitation_info


    @staticmethod
    def get_table_data(
        user: User | None,
        table: GatesTable,
        what_to_select: list[str] | None,
        params: dict | None,
        limit: int,
        offset: int,
        client_ip: Optional[str] = None
    ):
        """
        获取表格数据，包含权限检查、数据查询
        返回: (True, {what_to_select, table_data, total, hasMore}) 或 (False, '错误信息')
        """
                
        if not OpenGateService.can_read_table(user, table):
            username = user.username if user else 'anonymous'
            logger.error(f"[view_table_data] 没有查看权限 tid={table.tid} user={username} ip={client_ip}")
            return False, '没有查看权限'
        
        try:
            _require_table_autocommit()
            immudb_service = ImmudbService()
            with transaction.atomic():
                table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(table)
                for _ in range(3):
                    before = _table_version(table)
                    success, message = immudb_service.get_data(table.gate.gid, table.tid, limit, offset)
                    if not success:
                        logger.error(f"[get_table_data] 查询失败 tid={table.tid} {_ip_suffix(client_ip)} err={message}")
                        return False, message
                    cells = [
                        {'r': int(row[2]), 'c': int(row[3])}
                        for row in message.get('data', [])
                        if isinstance(row, (list, tuple)) and len(row) > 3
                    ]
                    versions = _cell_versions(table, cells)
                    after = _table_version(table)
                    if before == after:
                        message['cell_versions'] = {
                            f'{row}:{column}': version
                            for (row, column), version in versions.items()
                        }
                        message['table_version'] = after
                        return True, message
            return False, '表格正在更新，请重试'
        except Exception as e:
            logger.error(f"[get_table_data] 查询失败 tid={table.tid} {_ip_suffix(client_ip)} error={str(e)}")
            return False, f'查询失败: {str(e)}'
    

    @staticmethod
    def _can_read_table(user: User | None, table: GatesTable) -> bool:
        # 完整性证明属于既有公开透明能力，不返回表格业务数据。
        # A historical public child must not bypass an internal project.
        return OpenGateService.can_read_table(user, table)

    @staticmethod
    def get_table_integrity(user: User | None, tid: str, client_ip: Optional[str] = None):
        try:
            table = GatesTable.objects.get(tid=tid)
        except GatesTable.DoesNotExist:
            return False, 'table not found'

        if not OpenGateService._can_read_table(user, table):
            username = user.username if user else 'anonymous'
            logger.error(f"[table_integrity] no read permission tid={tid} user={username} ip={client_ip}")
            return False, 'authentication required' if user is None else 'no read permission'

        try:
            immudb_service = ImmudbService()
            success, data = immudb_service.get_table_integrity(table.gate.gid, table.tid)
            if not success:
                return False, data
            data.update({
                "table_id": table.tid,
                "last_write_time": _timestamp_to_iso(table.updated_at),
            })
            return True, data
        except Exception as e:
            logger.exception(f"[table_integrity] failed tid={tid} {_ip_suffix(client_ip)}")
            return False, f'integrity lookup failed: {str(e)}'

    @staticmethod
    def get_row_integrity(user: User | None, tid: str, row_id: str, client_ip: Optional[str] = None):
        try:
            table = GatesTable.objects.get(tid=tid)
        except GatesTable.DoesNotExist:
            return False, 'table not found'

        if not OpenGateService._can_read_table(user, table):
            username = user.username if user else 'anonymous'
            logger.error(f"[row_integrity] no read permission tid={tid} row_id={row_id} user={username} ip={client_ip}")
            return False, 'authentication required' if user is None else 'no read permission'

        try:
            immudb_service = ImmudbService()
            return immudb_service.get_row_integrity(table.gate.gid, table.tid, row_id)
        except Exception as e:
            logger.exception(f"[row_integrity] failed tid={tid} row_id={row_id} {_ip_suffix(client_ip)}")
            return False, f'row integrity lookup failed: {str(e)}'

    @staticmethod
    def verify_table_rows(user: User | None, tid: str, row_ids: list[str], client_ip: Optional[str] = None):
        try:
            table = GatesTable.objects.get(tid=tid)
        except GatesTable.DoesNotExist:
            return False, 'table not found'

        if not OpenGateService._can_read_table(user, table):
            username = user.username if user else 'anonymous'
            logger.error(f"[verify_table_rows] no read permission tid={tid} user={username} ip={client_ip}")
            return False, 'authentication required' if user is None else 'no read permission'

        try:
            immudb_service = ImmudbService()
            if not row_ids:
                success, summary = immudb_service.get_table_integrity(table.gate.gid, table.tid)
                if not success:
                    return False, summary
                verified_count = int(summary.get("verified_rows", 0) or 0)
                total_rows = int(summary.get("total_rows", 0) or 0)
                return True, {
                    "table_id": table.tid,
                    "verified_count": verified_count,
                    "failed_count": max(total_rows - verified_count, 0),
                    "merkle_root": summary.get("merkle_root"),
                    "integrity_score": summary.get("integrity_score"),
                    "results": [],
                }

            results = []
            verified_count = 0
            failed_count = 0
            for row_id in row_ids:
                success, proof = immudb_service.get_row_integrity(table.gate.gid, table.tid, row_id)
                result = {
                    "row_id": str(row_id),
                    "verified": bool(success and proof.get("verified")),
                }
                if success:
                    result.update({
                        "hash": proof.get("hash"),
                        "verification_time": proof.get("verification_time"),
                    })
                    verified_count += 1
                else:
                    result["message"] = proof
                    failed_count += 1
                results.append(result)

            return True, {
                "table_id": table.tid,
                "verified_count": verified_count,
                "failed_count": failed_count,
                "results": results,
            }
        except Exception as e:
            logger.exception(f"[verify_table_rows] failed tid={tid} {_ip_suffix(client_ip)}")
            return False, f'verification failed: {str(e)}'

    @staticmethod
    def upsert_table_data_full(
        user: User,
        table: GatesTable,
        values: dict,
        client_ip: Optional[str] = None,
        expected_table_version: int | None = None,
    ):
        """Preserve the raw cell payload under the managed write fence contract."""
        if not OpenGateService.can_write_table(user, table):
            logger.error(f"[upsert_table_data] 没有写入权限 tid={table.tid} user={user.username} ip={client_ip}")
            return False, '没有写入权限'
        serializer = WriteDataSerializer(data=values)
        if not serializer.is_valid():
            raise TableCellValidationError(serializer.errors)
        row_index, column_index = serializer.validated_data['r'], serializer.validated_data['c']
        if row_index < 0 or column_index < 0 or (table.num_columns and column_index >= table.num_columns):
            raise TableCellValidationError({'coordinates': ['单元格行列索引超出表格范围']})
        if expected_table_version is not None and (type(expected_table_version) is not int or expected_table_version < 0):
            raise TableCellValidationError({'expected_table_version': ['需要非负整数']})
        # Keep cell_id/type/format/style fields; this is not a plain value edit.
        payload = dict(values)
        _require_table_autocommit()
        baseline = _table_version(table) if expected_table_version is None else expected_table_version
        moderation_ok, moderation_msg = _moderate_opengate_texts(
            items=_table_write_text_items(payload),
            source=TextModerationAsset.SOURCE_EXTERNAL_API_TABLE_UPSERT,
            owner_type="table", owner_id=table.tid, actor_id=getattr(user, "uid", ""),
        )
        if not moderation_ok:
            return False, moderation_msg
        try:
            with transaction.atomic(durable=True):
                locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(locked_table)
                current = _table_version(locked_table)
                if current != baseline:
                    raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改', latest_table_version=current)
                fence = _start_table_audit(locked_table, user, TABLE_WRITE_FENCE_ACTION, client_ip,
                    details={'operation': 'data.upsert'})
            with transaction.atomic():
                locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(locked_table, fence.pk)
                current = _table_version(locked_table)
                if current != baseline:
                    raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改', latest_table_version=current)
                event = _start_table_audit(locked_table, user, 'data.upsert', client_ip,
                    row_index=row_index, column_index=column_index, details={'fields': list(payload.keys())[:100]})
                success, message = ImmudbService().upsert_data(locked_table.gate.gid, locked_table.tid, payload)
                if success is not True:
                    raise UnconfirmedWriteError()
                _finish_table_audit(event, True)
                locked_table.updated_at = str(time.time())[:20]
                locked_table.save(update_fields=['updated_at'])
                _finish_table_audit(fence, True)
                return True, message
        except (TableWriteConflict, UnconfirmedWriteError):
            raise
        except Exception as exc:
            logger.exception("[upsert_table_data_full] unconfirmed tid=%s", table.tid)
            raise UnconfirmedWriteError() from exc
    
    @staticmethod
    def _normalize_cell_value(value: Any) -> tuple[str, str]:
        """Return the string payload and Immudb cell type for the given value."""
        if value is None:
            return "", CellCttChoices.GENERAL
        if isinstance(value, bool):
            return ("1" if value else "0"), CellCttChoices.BOOLEAN
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value), CellCttChoices.NUMBER
        text = str(value)
        if isinstance(value, str) and text.startswith("="):
            return text, CellCttChoices.FORMULA
        return text, CellCttChoices.STRING

    @staticmethod
    def _prepare_cell_payload(cell: dict[str, Any]) -> dict[str, Any]:
        """Build the payload that ImmudbService.upsert_data expects."""
        cts, ctt = OpenGateService._normalize_cell_value(cell.get("value"))
        return {
            "cell_id": None,
            "r": cell["r"],
            "c": cell["c"],
            "ctfa": CellCtfaChoices.GENERAL,
            "ctt": ctt,
            "cts": cts,
        }

    @staticmethod
    def _ensure_cell_dict(cell: Any) -> dict[str, Any]:
        """Normalize legacy tuple/list payloads into dicts."""
        if isinstance(cell, (tuple, list)):
            r = cell[0] if len(cell) > 0 else None
            c = cell[1] if len(cell) > 1 else None
            value = cell[2] if len(cell) > 2 else None
            return {"r": r, "c": c, "value": value}
        if isinstance(cell, dict):
            return cell
        raise ValueError("Unsupported cell payload type")

    @staticmethod
    def upsert_table_cells(
        user: User,
        table: GatesTable,
        cells: list[dict[str, Any]],
        client_ip: Optional[str] = None,
        api_key: APIKey | None = None,
        append: bool = False,
        expected_max_row_index: int | None = None,
        expected_table_version: int | None = None,
    ):
        """Persist cell edits, with optimistic checks for concurrent writers."""
        if not OpenGateService.can_write_table(user, table):
            logger.error(f"[upsert_table_cells] no write permission tid={table.tid} user={user.username} ip={client_ip}")
            return False, '没有写入权限'

        _require_table_autocommit()
        try:
            normalized_cells = [OpenGateService._ensure_cell_dict(cell) for cell in cells]
            coordinates = [(cell['r'], cell['c']) for cell in normalized_cells]
            if len(set(coordinates)) != len(coordinates):
                return False, '同一请求不能重复写入相同单元格'
            if table.num_columns and any(column >= table.num_columns for _, column in coordinates):
                return False, '列索引超出表格范围'
            if append and (
                expected_max_row_index is None
                or len({row for row, _ in coordinates}) != 1
                or coordinates[0][0] != expected_max_row_index + 1
            ):
                return False, '新增行参数无效'

            payloads = [OpenGateService._prepare_cell_payload(cell) for cell in normalized_cells]
            for payload in payloads:
                serializer = WriteDataSerializer(data=payload)
                if not serializer.is_valid():
                    raise TableCellValidationError(serializer.errors)

            # Legacy callers without a revision still get a snapshot before waiting
            # on the table lock, so overlapping writes cannot silently overwrite.
            baseline_versions = _cell_versions(table, normalized_cells) if not append else {}
            baseline_table_version = _table_version(table)
            moderation_ok, moderation_msg = _moderate_opengate_texts(
                items=_table_write_text_items(normalized_cells),
                source=TextModerationAsset.SOURCE_EXTERNAL_API_TABLE_UPSERT,
                owner_type="table",
                owner_id=table.tid,
                actor_id=getattr(user, "uid", ""),
            )
            if not moderation_ok:
                return False, moderation_msg
            immudb_service = ImmudbService()
            with transaction.atomic(durable=True):
                locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(locked_table)
                if append:
                    ok, summary = immudb_service.get_data(table.gate.gid, table.tid, 1, 0)
                    if not ok:
                        return False, summary
                    latest_row = summary['max_row_index']
                    if latest_row != expected_max_row_index:
                        raise TableWriteConflict(
                            'ROW_APPEND_CONFLICT', '表格尾部已被其他用户更新',
                            latest_max_row_index=latest_row,
                            next_row_index=latest_row + 1,
                        )
                current_table_version = _table_version(locked_table)
                if current_table_version != (expected_table_version if expected_table_version is not None else baseline_table_version):
                    raise TableWriteConflict(
                        'TABLE_WRITE_CONFLICT', '表格已被其他用户修改',
                        latest_table_version=current_table_version,
                    )
                if not append:
                    current_versions = _cell_versions(locked_table, normalized_cells)
                    for cell in normalized_cells:
                        coordinate = (cell['r'], cell['c'])
                        expected = cell.get('expected_version', baseline_versions[coordinate])
                        current = current_versions[coordinate]
                        if expected != current:
                            raise TableWriteConflict(
                                'CELL_WRITE_CONFLICT', '单元格已被其他用户修改',
                                row_index=cell['r'], column_index=cell['c'],
                                latest_version=current,
                            )

                fence = _start_table_audit(
                    locked_table, user, TABLE_WRITE_FENCE_ACTION, client_ip, api_key,
                    details={'operation': 'cell.upsert'},
                )
            # The fence has committed before any remote mutation. A crash or
            # rollback below leaves it pending; only full success can finish it.
            with transaction.atomic():
                locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(locked_table, fence.pk)
                if _table_version(locked_table) != current_table_version:
                    raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改')
                events = [
                    _start_table_audit(
                        locked_table, user, 'cell.upsert', client_ip, api_key,
                        row_index=cell['r'], column_index=cell['c'],
                        details={'value_type': type(cell.get('value')).__name__},
                    )
                    for cell in normalized_cells
                ]
                if append:
                    success, message = immudb_service.insert_row(table.gate.gid, table.tid, payloads)
                    for event in events:
                        _finish_table_audit(event, success)
                    if not success:
                        return False, message
                else:
                    for cell, payload, event in zip(normalized_cells, payloads, events):
                        success, message = immudb_service.upsert_data(table.gate.gid, table.tid, payload)
                        _finish_table_audit(event, success)
                        if not success:
                            logger.error(f"[upsert_table_cells] insert failed tid={table.tid} cell={cell} err={message}")
                            return False, message

                locked_table.updated_at = str(time.time())[:20]
                locked_table.save(update_fields=['updated_at'])
                _finish_table_audit(fence, True)
                return True, {
                    'row_index': coordinates[0][0],
                    'table_version': max(event.id for event in events),
                    'cell_versions': {
                        f'{event.row_index}:{event.column_index}': event.id
                        for event in events
                    },
                }
        except TableCellValidationError:
            raise
        except UnconfirmedWriteError:
            # Preserve uncertainty across the ORM/remote-store boundary.
            raise
        except TableWriteConflict:
            raise
        except Exception as e:
            logger.exception(f"[upsert_table_cells] 写入失败 tid={table.tid} {_ip_suffix(client_ip)}")
            return False, f'写入失败: {str(e)}'

    @staticmethod
    def delete_table_row(
        user: User,
        table: GatesTable,
        row_index: int,
        client_ip: Optional[str] = None,
        expected_table_version: int | None = None,
    ):
        if not OpenGateService.can_write_table(user, table):
            return False, '没有写入权限'
        if type(row_index) is not int or row_index < 0:
            raise TableCellValidationError({'row_index': ['需要非负整数']})
        if expected_table_version is not None and (type(expected_table_version) is not int or expected_table_version < 0):
            raise TableCellValidationError({'expected_table_version': ['需要非负整数']})
        _require_table_autocommit()
        baseline = _table_version(table) if expected_table_version is None else expected_table_version
        try:
            with transaction.atomic(durable=True):
                locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(locked_table)
                current = _table_version(locked_table)
                if current != baseline:
                    raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改', latest_table_version=current)
                fence = _start_table_audit(locked_table, user, TABLE_WRITE_FENCE_ACTION, client_ip,
                    details={'operation': 'row.delete'})
            with transaction.atomic():
                locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
                _assert_table_write_fence(locked_table, fence.pk)
                current = _table_version(locked_table)
                if current != baseline:
                    raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改', latest_table_version=current)
                event = _start_table_audit(locked_table, user, 'row.delete', client_ip, row_index=row_index)
                success, result = ImmudbService().delete_row(locked_table.gate.gid, locked_table.tid, row_index)
                if success is not True:
                    # False includes absent rows and ambiguous remote failures;
                    # do not infer a safe retry from its message.
                    raise UnconfirmedWriteError()
                event.details = {'deleted_cells': result}
                event.save(update_fields=['details'])
                _finish_table_audit(event, True)
                locked_table.updated_at = str(time.time())[:20]
                locked_table.save(update_fields=['updated_at'])
                _finish_table_audit(fence, True)
                return True, result
        except (TableWriteConflict, UnconfirmedWriteError):
            raise
        except Exception as exc:
            logger.exception("[delete_table_row] unconfirmed tid=%s row=%s", table.tid, row_index)
            raise UnconfirmedWriteError() from exc

    @staticmethod
    def clear_table_cells(
        user: User,
        table: GatesTable,
        cells: list[dict[str, int]],
        client_ip: Optional[str] = None,
        expected_table_version: int | None = None,
    ):
        """Check the read revision under the same table lock used by upserts."""
        if not OpenGateService.can_write_table(user, table):
            return False, '没有写入权限'
        _require_table_autocommit()
        baseline = _table_version(table) if expected_table_version is None else expected_table_version
        with transaction.atomic(durable=True):
            locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
            _assert_table_write_fence(locked_table)
            current = _table_version(locked_table)
            if current != baseline:
                raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改',
                    latest_table_version=current)
            fence = _start_table_audit(locked_table, user, TABLE_WRITE_FENCE_ACTION, client_ip,
                details={'operation': 'cell.clear'})
        with transaction.atomic():
            locked_table = GatesTable.objects.select_for_update().get(pk=table.pk)
            _assert_table_write_fence(locked_table, fence.pk)
            current = _table_version(locked_table)
            if current != baseline:
                raise TableWriteConflict('TABLE_WRITE_CONFLICT', '表格已被其他用户修改',
                    latest_table_version=current)
            result = OpenGateService._clear_table_cells_locked(user, locked_table, cells, client_ip)
            if result[0] is True:
                _finish_table_audit(fence, True)
            return result

    @staticmethod
    def _clear_table_cells_locked(
        user: User,
        table: GatesTable,
        cells: list[dict[str, int]],
        client_ip: Optional[str] = None,
    ):
        """Clear cell values and audit each requested cell coordinate."""
        if not OpenGateService.can_write_table(user, table):
            logger.error(f"[clear_table_cells] no write permission tid={table.tid} user={user.username} ip={client_ip}")
            return False, '没有写入权限'

        audit_events = [
            (
                cell,
                _start_table_audit(
                    table, user, 'cell.clear', client_ip,
                    row_index=cell['r'], column_index=cell['c'],
                ),
            )
            for cell in cells
        ]
        try:
            immudb_service = ImmudbService()
            success, result = immudb_service.clear_cells(table.gate.gid, table.tid, cells)
            cleared_cells = result.get('cleared_cells', []) if isinstance(result, dict) else []
            cleared_coordinates = {
                (cell.get('r'), cell.get('c'))
                for cell in cleared_cells
                if isinstance(cell, dict)
            }
            for cell, event in audit_events:
                coordinate = (cell['r'], cell['c'])
                changed = coordinate in cleared_coordinates
                event.details = {'cleared': changed}
                event.result = (
                    TableAuditEvent.RESULT_SUCCESS
                    if success or changed
                    else TableAuditEvent.RESULT_FAILED
                )
                event.save(update_fields=['details', 'result'])
            if not success:
                message = result.get('message', '清空单元格失败') if isinstance(result, dict) else result
                logger.error(f"[clear_table_cells] failed tid={table.tid} err={message}")
                return False, message
            return True, len(cleared_coordinates)
        except Exception as exc:
            for _, event in audit_events:
                event.details = {'cleared': False}
                event.result = TableAuditEvent.RESULT_FAILED
                event.save(update_fields=['details', 'result'])
            logger.exception(f"[clear_table_cells] 清空失败 tid={table.tid} {_ip_suffix(client_ip)}")
            return False, f'清空失败: {exc}'

    @staticmethod
    def create_gate_key(user: User, gate: Gates, key_name: str, client_ip: Optional[str] = None):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_PROJECT, external_id=gate.gid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN):
            return False, '用户无权限创建API key'
        try:
            if APIKey.objects.filter(gate=gate, is_active=1).count() >= 20:
                return False, "应用已达到最大API key数量"
            from common.utils.Login_utils import generate_api_key_pair as pair  # reuse existing
            secret_id, secret_key, secret_verification = pair()
            created_at = str(time.time())[:20]
            APIKey.objects.create(gate=gate, user=user, secret_id=secret_id, key_name=key_name, secret_verification=secret_verification, is_active=1, created_at=created_at)
            return True, {"secret_id": secret_id, "secret_key": secret_key, "created_at": created_at}
        except Exception as e:
            logger.error(f"create_gate_key failed:{_ip_suffix(client_ip)} err={e}")
            return False, "创建失败"

    @staticmethod
    def delete_gate_key(user: User, api_key: APIKey, gate: Gates, client_ip: Optional[str] = None):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_PROJECT, external_id=gate.gid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN):
            return False, '用户无权限删除API key'
        api_key.delete()
        return True, '删除成功'

    @staticmethod
    def list_tables_by_user(user: User, limit: int, offset: int):
        resource_ids = visible_resource_ids(user, Resource.TYPE_TABLE)
        tables = GatesTable.objects.filter(tid__in=resource_ids)
        total = tables.count()
        tables = list(tables.order_by('id')[offset: offset + limit])
        # 将 GatesTable 对象转换为可序列化的字典
        tables_data = [table.get_info() for table in tables]
        has_more = (offset + limit) < total
        return {'total': total, 'tables': tables_data, 'hasMore': has_more}

    @staticmethod
    def toggle_gate_key(user: User, gid: str, secret_id: str, client_ip: Optional[str] = None):
        try:
            gate = Gates.objects.get(gid=gid)
        except Gates.DoesNotExist:
            logger.error(f"toggle_gate_key failed: gate not found gid={gid} {_ip_suffix(client_ip)}")
            return False, '应用不存在'
        
        try:
            api_key = APIKey.objects.get(secret_id=secret_id, gate=gate)
        except APIKey.DoesNotExist:
            logger.error(f"toggle_gate_key failed: API key not found secret_id={secret_id} gid={gid} {_ip_suffix(client_ip)}")
            return False, 'API key不存在'
        
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_PROJECT, external_id=gate.gid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.PERM_ADMIN):
            logger.error(f"toggle_gate_key failed: no permission user={user.username} gid={gid} {_ip_suffix(client_ip)}")
            return False, '用户无权限暂停API key'
        api_key.toggle_status()
        return True, '暂停成功'

    @staticmethod
    def update_table_member(user: User, tid: str, uid: str, permission: int, client_ip: Optional[str] = None):
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_TABLE, external_id=tid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.ROLE_MANAGER):
            return False, '用户无权限管理该表格'
        if permission not in [Grant.PERM_READ, Grant.PERM_WRITE, Grant.PERM_ADMIN]:
            return False, '权限错误'

        target_user = User.objects.get(uid=uid)
        target_principal = get_or_create_principal(Principal.TYPE_USER, external_id=target_user.uid)
        if is_granted(principal=target_principal, resource=resource, permissions=Grant.PERM_SUPER_ADMIN):
            return False, '用户无权限修改该表格成员，因为该成员是超级管理员'

        grant = grant_permissions(principal=target_principal, resource=resource, permissions=permission, granter_user=user)
        if not grant:
            logger.error(f"update_table_member failed: user={user.username} tid={tid} uid={uid} permission={permission} client_ip={client_ip}")
            return False, '权限修改失败'
        logger.info(f"update_table_member success: user={user.username} tid={tid} uid={uid} permission={permission} client_ip={client_ip}")
        return True, '权限修改成功'

    @staticmethod
    def remove_table_member(user: User, tid: str, uid: str, client_ip: Optional[str] = None):
        if user.uid == uid:
            return False, '不能删除自己'
        principal = get_or_create_principal(Principal.TYPE_USER, external_id=user.uid)
        resource = get_or_create_resource(Resource.TYPE_TABLE, external_id=tid)
        if not is_granted(principal=principal, resource=resource, permissions=Grant.ROLE_MANAGER):
            return False, '用户无权限管理该表格'
        target_user = User.objects.get(uid=uid)
        target_principal = get_or_create_principal(Principal.TYPE_USER, external_id=target_user.uid)
        if is_granted(principal=target_principal, resource=resource, permissions=Grant.PERM_SUPER_ADMIN):
            return False, '用户无权限删除该表格成员，因为该成员是超级管理员'
        revoke_permissions(principal=target_principal, resource=resource)
        logger.info(f"remove_table_member success: user={user.username} tid={tid} uid={uid} client_ip={client_ip}")
        return True, '删除成功'
