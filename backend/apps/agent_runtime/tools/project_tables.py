"""Agent tools for company-scoped OpenGate projects and tables."""

from __future__ import annotations

from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.agent_runtime.contracts import ToolContext, ToolRequest
from apps.company.models import Company, TransparentCompanyAgentDefinition
from apps.iam.models import Grant, Resource, Tenant
from apps.iam.services import runtime_action_allowed
from apps.opengate.models import Gates, GatesTable
from apps.opengate.services import OpenGateService
from common.immudbIO.errors import UnconfirmedWriteError
from apps.user.models import User


class ProjectTablePermissionDenied(PermissionError):
    pass


def _agent_external_id(company: Company, agent: TransparentCompanyAgentDefinition) -> str:
    return f"agent:{company.cid}:{agent.agentId}"


def _tenant(company: Company):
    return Tenant.objects.filter(
        type=Tenant.TYPE_ORGANIZATION,
        external_id=f"organization:{company.cid}",
        status=Tenant.STATUS_ACTIVE,
    ).first()


def agent_project_table_scopes(company: Company, agent: TransparentCompanyAgentDefinition) -> dict[str, list[str]]:
    """Return current Agent read/write scopes for company-labeled projects."""
    tenant = _tenant(company)
    empty = {"projects_read": [], "tables_read": [], "tables_write": []}
    if tenant is None:
        return empty
    from apps.iam.models import Principal

    principal = Principal.objects.filter(
        type=Principal.TYPE_AGENT,
        external_id=_agent_external_id(company, agent),
    ).first()
    if principal is None:
        return empty
    projects = list(Gates.objects.filter(
        companygate__company=company,
        label=Gates.LABEL_COMPANY,
    ).distinct())
    project_ids = [item.gid for item in projects]
    tables = list(GatesTable.objects.filter(gate__gid__in=project_ids).only("tid", "gate_id")) if project_ids else []
    external_ids = project_ids + [item.tid for item in tables]
    resources = {
        resource.external_id: resource
        for resource in Resource.objects.filter(
            tenant=tenant,
            external_id__in=external_ids,
            type__in=(Resource.TYPE_PROJECT, Resource.TYPE_TABLE),
        ).select_related("parent")
    }
    grants = {
        grant.resource_id: grant
        for grant in Grant.objects.filter(
            principal=principal,
            resource_id__in=[resource.id for resource in resources.values()],
        ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
    }
    project_role_by_id = {}
    project_reads = []
    table_reads = []
    table_writes = []
    for project in projects:
        project_resource = resources.get(project.gid)
        project_grant = grants.get(project_resource.id) if project_resource else None
        project_role = min(int(project_grant.permissions), 2) if project_grant else 0
        if project_resource:
            project_role_by_id[project_resource.id] = project_role
        if project_resource and project_role >= 1:
            project_reads.append(project.gid)
    for table in tables:
        resource = resources.get(table.tid)
        if resource is None:
            continue
        direct_grant = grants.get(resource.id)
        if direct_grant is not None:
            permission = min(int(direct_grant.permissions), 2)
        elif resource.permission_mode == Resource.PERMISSION_MODE_INDEPENDENT or not resource.parent_id:
            permission = 0
        else:
            parent_grant = grants.get(resource.parent_id)
            permission = (
                project_role_by_id.get(resource.parent_id, 0)
                if parent_grant and parent_grant.inherit
                else 0
            )
        if permission >= 1:
            table_reads.append(table.tid)
        if permission >= 2:
            table_writes.append(table.tid)
    return {
        "projects_read": project_reads,
        "tables_read": table_reads,
        "tables_write": table_writes,
    }


def _resolve_context(context: ToolContext):
    company = Company.objects.filter(pk=context.company_id).first()
    agent = TransparentCompanyAgentDefinition.objects.filter(
        pk=context.agent_id,
        transparentCompany__company=company,
    ).first() if company else None
    user = User.objects.filter(pk=context.user_id).first() if context.user_id else None
    if company is None or agent is None or user is None:
        raise ProjectTablePermissionDenied("当前 Agent 或操作者身份无效")
    tenant = _tenant(company)
    if tenant is None:
        raise ProjectTablePermissionDenied("公司资源目录不可用")
    return company, agent, user, tenant, _agent_external_id(company, agent)


def _resource(tenant, resource_type: str, external_id: str):
    return Resource.objects.filter(
        tenant=tenant,
        type=resource_type,
        external_id=external_id,
    ).select_related("parent").first()


def _table_scope(table: GatesTable) -> str:
    return {
        GatesTable.PERM_PRIVATE: "private",
        GatesTable.PERM_READ: "public_read",
        GatesTable.PERM_WRITE: "public_write",
    }.get(table.permission, "private")


def agent_project_table_permission_facts(
    company: Company,
    agent: TransparentCompanyAgentDefinition,
    user: User | None,
) -> list[dict[str, Any]]:
    """Return only project/table access the current user and Agent can use."""
    if user is None:
        return []
    tenant = _tenant(company)
    if tenant is None:
        return []

    projects = list(
        Gates.objects.filter(companygate__company=company, label=Gates.LABEL_COMPANY)
        .distinct().order_by("id")
    )
    if not projects:
        return []
    project_ids = [project.id for project in projects]
    tables = list(
        GatesTable.objects.filter(gate_id__in=project_ids)
        .only("id", "tid", "name", "gate_id", "permission")
        .order_by("id")
    )
    external_ids = [project.gid for project in projects] + [table.tid for table in tables]
    resources = {
        (resource.type, resource.external_id): resource
        for resource in Resource.objects.filter(
            tenant=tenant,
            external_id__in=external_ids,
            type__in=(Resource.TYPE_PROJECT, Resource.TYPE_TABLE),
        ).select_related("parent")
    }
    tables_by_gate: dict[int, list[GatesTable]] = {}
    for table in tables:
        tables_by_gate.setdefault(table.gate_id, []).append(table)

    agent_external_id = _agent_external_id(company, agent)
    facts = []
    for project in projects:
        project_resource = resources.get((Resource.TYPE_PROJECT, project.gid))
        project_readable = bool(
            project_resource
            and runtime_action_allowed(user, agent_external_id, project_resource, "view")
        )
        readable_tables = []
        for table in tables_by_gate.get(project.id, []):
            resource = resources.get((Resource.TYPE_TABLE, table.tid))
            if resource is None or not runtime_action_allowed(
                user, agent_external_id, resource, "read_rows",
            ):
                continue
            can_write = runtime_action_allowed(
                user, agent_external_id, resource, "update_rows",
            )
            readable_tables.append({
                "name": table.name,
                "scope": _table_scope(table),
                "can_read": True,
                "can_write": can_write,
                "can_clear_cells": can_write,
            })
        if project_readable or readable_tables:
            facts.append({
                "name": project.gatename,
                "can_read": project_readable,
                "tables": readable_tables,
            })
    return facts


def execute_project_table_tool(request: ToolRequest, context: ToolContext) -> dict[str, Any]:
    company, agent, user, tenant, agent_external_id = _resolve_context(context)
    if request.name == "company.project_tables.list":
        projects = list(Gates.objects.filter(
            companygate__company=company,
            label=Gates.LABEL_COMPANY,
        ).distinct().order_by("id"))
        project_ids = [item.id for item in projects]
        tables = list(GatesTable.objects.filter(gate_id__in=project_ids).only("id", "tid", "name", "gate_id", "permission").order_by("id")) if project_ids else []
        resources = {
            resource.external_id: resource
            for resource in Resource.objects.filter(
                tenant=tenant,
                external_id__in=[item.gid for item in projects] + [item.tid for item in tables],
                type__in=(Resource.TYPE_PROJECT, Resource.TYPE_TABLE),
            ).select_related("parent")
        }
        tables_by_gate = {}
        for table in tables:
            tables_by_gate.setdefault(table.gate_id, []).append(table)
        results = []
        for project in projects:
            project_resource = resources.get(project.gid)
            project_allowed = bool(project_resource and runtime_action_allowed(
                user, agent_external_id, project_resource, "view",
            ))
            table_items = []
            for table in tables_by_gate.get(project.id, []):
                table_resource = resources.get(table.tid)
                if table_resource is None or not runtime_action_allowed(
                    user, agent_external_id, table_resource, "read_rows",
                ):
                    continue
                can_write = runtime_action_allowed(user, agent_external_id, table_resource, "update_rows")
                table_items.append({
                    "tid": table.tid,
                    "name": table.name,
                    "scope": _table_scope(table),
                    "permission": _table_scope(table),
                    "agent_permission": "read_write" if can_write else "read_only",
                    "can_write": can_write,
                    "can_clear_cells": can_write,
                })
            if project_allowed or table_items:
                results.append({
                    "gid": project.gid,
                    "name": project.gatename,
                    "tables": table_items,
                })
        return {"tool_name": request.name, "projects": results}

    tid = str(request.arguments.get("tid") or "").strip()
    table = GatesTable.objects.filter(
        tid=tid,
        gate__label=Gates.LABEL_COMPANY,
        gate__companygate__company=company,
    ).select_related("gate").first()
    if table is None:
        raise FileNotFoundError("公司项目表格不存在")
    resource = _resource(tenant, Resource.TYPE_TABLE, table.tid)
    if resource is None:
        raise ProjectTablePermissionDenied("公司表格资源目录不可用")

    if request.name == "company.table.read":
        if not runtime_action_allowed(user, agent_external_id, resource, "read_rows"):
            raise ProjectTablePermissionDenied("当前 Agent 无权读取该表格")
        limit = max(1, min(int(request.arguments.get("limit", 50)), 100))
        offset = max(0, int(request.arguments.get("offset", 0)))
        ok, data = OpenGateService.get_table_data(
            user=user,
            table=table,
            what_to_select=None,
            params={},
            limit=limit,
            offset=offset,
        )
        if not ok:
            raise ValueError(str(data))
        return {
            "tool_name": request.name,
            "tid": table.tid,
            "project": table.gate.gatename,
            "table": table.name,
            "scope": _table_scope(table),
            "data": data,
        }

    if request.name == "company.table.write":
        if not runtime_action_allowed(user, agent_external_id, resource, "update_rows"):
            raise ProjectTablePermissionDenied("当前 Agent 无权写入该表格")
        cells = request.arguments.get("cells")
        if not isinstance(cells, list) or not cells:
            raise ValueError("cells 必须是非空数组")
        for cell in cells:
            if not isinstance(cell, dict) or not isinstance(cell.get("r"), int) or not isinstance(cell.get("c"), int) or "value" not in cell:
                raise ValueError("每个单元格需要整数 r、整数 c 和 value")
            if cell["r"] < 0 or cell["c"] < 0 or cell["c"] >= int(table.num_columns or 0):
                raise ValueError("单元格行列索引超出表格范围")
            value = cell["value"]
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("单元格 value 仅支持文本、数字、布尔值或空值")
        ok, message = OpenGateService.upsert_table_cells(
            user=user,
            table=table,
            cells=cells,
        )
        if not ok:
            # Legacy False can mean partial remote effects, not "nothing happened".
            raise UnconfirmedWriteError()
        return {"tool_name": request.name, "tid": table.tid, "updated_cells": len(cells), "message": str(message)}

    if request.name == "company.table.clear_cells":
        if not runtime_action_allowed(user, agent_external_id, resource, "update_rows"):
            raise ProjectTablePermissionDenied("当前 Agent 无权清空该表格单元格")
        cells = request.arguments.get("cells")
        if not isinstance(cells, list) or not cells or len(cells) > 100:
            raise ValueError("cells 必须是 1 到 100 项的数组")
        coordinates = []
        seen_coordinates = set()
        for cell in cells:
            if (
                not isinstance(cell, dict)
                or type(cell.get("r")) is not int
                or type(cell.get("c")) is not int
            ):
                raise ValueError("每个单元格需要整数 r 和整数 c")
            row, column = cell["r"], cell["c"]
            if row < 0 or column < 0 or column >= int(table.num_columns or 0):
                raise ValueError("单元格行列索引超出表格范围")
            coordinate = (row, column)
            if coordinate in seen_coordinates:
                raise ValueError("cells 中不能重复指定同一单元格")
            seen_coordinates.add(coordinate)
            coordinates.append({"r": row, "c": column})
        ok, cleared_count = OpenGateService.clear_table_cells(
            user=user,
            table=table,
            cells=coordinates,
        )
        if not ok:
            raise UnconfirmedWriteError()
        return {
            "tool_name": request.name,
            "tid": table.tid,
            "cleared_cells": cleared_count,
            "message": f"已清空 {cleared_count} 个单元格内容，表格结构未改变",
        }

    raise ValueError("未知项目表格工具")
