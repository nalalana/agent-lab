"""
/backend/apps/opengate/urls.py
OpenGate URL路由
""" 
from django.urls import path
from apps.opengate.views import *

urlpatterns = [
    # Gate 管理
    path('gates/create', create_gate, name='gates'),  # POST 创建Gate（规范）
    path('gates/update', update_gate, name='gates_update'),  # POST 更新Gate（规范）
    path('gates/public-access', update_gate_public_access, name='gates_public_access'),
    path('gates/delete', delete_gate, name='gates_delete'),
    path('gates/upload-image', upload_gate_image, name='gates_upload_image'),
    path('gates/transfer-owner', transfer_owner, name='gates_transfer_owner'), #POST 转移Gate所有权（规范）TODO:一期暂时不上线该功能，暂未对其做出修改
    path('gates/list', list_gate, name='gates_list'),  # POST 获取Gate列表（规范）
    path('gates/details', view_gate_details, name='gates_details'), #POST 获取Gate详情（规范）
    path('gates/search', search_gate, name='gates_search'),

    # Table 管理
    path('tables', create_table, name='tables'),  # POST 创建表（规范）
    path('tables/update', update_table, name='tables_update'),
    path('gates/tables', list_table_by_gate, name='gates_tables'),  # POST 获取Gate的表（规范）
    path('tables/list', list_table_by_user, name='tables_list_by_user'),
    path('tables/details', view_table_details, name='tables_details'),
    path('tables/<str:table_id>/integrity', table_integrity, name='tables_integrity'),
    path('tables/<str:table_id>/rows/<str:row_id>/integrity', row_integrity, name='tables_rows_integrity'),
    path('tables/<str:table_id>/verify', verify_table_integrity, name='tables_verify_integrity'),

    # 成员与邀请
    path('tables/members/list', list_table_member, name='tables_members_list'),
    path('tables/members/add', add_table_member, name='tables_members_add'),
    path('tables/members/remove', remove_table_member, name='tables_members_remove'),


    # 数据访问与写入
    path('tables/data', view_table_data, name='tables_data'),
    path('tables/audit', table_audit_events, name='tables_audit'),
    path('tables/data/upsert', upsert_table_cells, name='tables_data_upsert'),
    path('tables/data/rows/delete', delete_table_row, name='tables_data_row_delete'),
    path('tables/upsert', upsert_table_data, name='tables_upsert'),

    # App 访问（对接外部应用场景）
    path('app/tables/details', app_table_details, name='app_tables_details'), #TODO:废弃该接口，以后表格详情信息从iam app获取
    path('app/tables/create', app_create_table, name='app_tables_create'),
    path('app/tables/data', app_table_data, name='app_tables_data'),
    path('app/tables/upsert', app_upsert_table_data, name='app_tables_upsert'),

    # 权限
    path('tables/permissions', get_permission, name='tables_permissions'), #TODO:废弃该接口，以后所有权限信息从iam app获取

    # API Key 管理
    path('keys/create', create_gate_key, name='gates_keys_create'),
    path('keys/delete', delete_gate_key, name='gates_keys_delete'),
    path('keys', list_gate_key, name='gates_keys_list'),
    path('keys/toggle', toggle_gate_key, name='gates_keys_toggle'), # 暂停api key
    path('keys/test', api_key_test, name='gates_keys_test'),  #TODO:废弃该接口
]


