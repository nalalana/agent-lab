from django.urls import path
from .views import check_access, grant, revoke, list_grants, effective, list_grants_by_principal
from . import views_unified, views_organization

urlpatterns = [
    path('unified/organization', views_organization.organization_tree),
    path('unified/organization/member/add', views_organization.organization_member_add),
    path('unified/organization/members/update', views_organization.organization_members_update),
    path('check-access', check_access),
    path('unified/context', views_unified.unified_context),
    path('unified/resources', views_unified.unified_resources),
    path('unified/agent-project-tables', views_unified.unified_agent_project_tables),
    path('unified/agent-project-table-permission', views_unified.unified_agent_project_table_permission_upsert),
    path('unified/resource-grants', views_unified.unified_resource_grants),
    path('unified/grant/upsert', views_unified.unified_grant_upsert),
    path('unified/grant/revoke', views_unified.unified_grant_revoke),
    path('unified/groups', views_unified.unified_groups),
    path('unified/group/upsert', views_unified.unified_group_upsert),
    path('unified/group/delete', views_unified.unified_group_delete),
    path('unified/group/member', views_unified.unified_group_member),
    path('unified/permission-mode', views_unified.unified_permission_mode),
    path('unified/explain', views_unified.unified_explain),
    # path('grant', grant),
    # path('revoke', revoke),
    # path('list', list_grants),
    # path('list_by_principal', list_grants_by_principal),
    # path('effective', effective),
]
