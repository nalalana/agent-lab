## 用户相关数据表
user_format = {
    "id": "INTEGER",
    "username": "VARCHAR",
    "email": "VARCHAR",
    "phone_number": "VARCHAR",
    "user_password": "VARCHAR",
    "avatar_url": "VARCHAR", # 用户头像URL
    "bio": "TEXT", # 用户个人简介
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
}

## team相关数据表
team_format = {
    "id": "INTEGER",
    "team_name": "VARCHAR",
    "creator": "USER",
    "description": "VARCHAR",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
}
team_members_format = {
    "team": "TEAM",
    "member": "USER",
    "permission_level": "VARCHAR", # 管理员、普通成员
    "is_valid": "INTEGER"   # -1代表用户拒绝或已被删除，0代表已发送邀请请求等待确认，1代表用户已加入
}


#table or collection相关数据表
user_table_format = {
    "id": "INTEGER",
    "table_name": "VARCHAR",
    "description": "TEXT",
    "owner": "USER",
    "permission_level": "VARCHAR",
    "headers":"JSON",   # {'name':'', 'age':'', 'salary':''}
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
}

table_content_format = {
    "id": "INTEGER",
    "table": "TABLE",
    "content": "JSON",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
}


table_member_format = {
    "id": "INTEGER",
    "member": "USER",
    "team": "TEAM",
    "table": "USERTABLE",
    "permission_level": "VARCHAR",
    "is_valid": "INT",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
}


# 项目相关数据表
projects_format = {
    "id": "INTEGER",
    "name": "VARCHAR",
    "description": "TEXT",
    "owner_id": "INTEGER",
    "visibility": "VARCHAR",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
},
project_repos_format = {
    "id": "INTEGER",
    "project_id": "INTEGER",
    "table_id": "INTEGER",
    "created_at": "TIMESTAMP"
},
project_permission_format = {
    "id": "INTEGER",
    "project_id": "INTEGER",
    "user_id": "INTEGER",
    "team_id": "INTEGER",
    "create_at": "TIMESTAMP"
}


# 智能合约相关数据表
smart_contracts_format = {
    "id": "INTEGER",
    "name": "VARCHAR",  # 合约名称
    "description": "TEXT",  # 合约描述
    "is_private": "BOOL",
    "owner": "INTEGER",  # 创建合约的用户ID（关联 users 表）
    "contract_code": "TEXT",  # 智能合约代码
    "contract_hash": "VARCHAR",
    "created_at": "TIMESTAMP",
}

contract_member_format = {
    "id": "INTEGER",
    "member": "USER",
    "team": "TEAM",
    "contract": "SMARTCONTRACT",
    "permission_level": "VARCHAR",
    "is_valid": "INT",
    "created_at": "VARCHAR",
    "updated_at": "VARCHAR"
}

contract_execution_logs_format = {
    "id": "INTEGER",
    "contract": "SMART_CONTRACT",  # 关联 smart_contracts 表
    "execution_hash": "VARCHAR",
    "execution_status": "VARCHAR",  # 执行状态（如 'success', 'failed'）
    "execution_result": "TEXT",  # 执行结果或错误信息
    "executed_at": "TIMESTAMP"
}


contract_variables_format = {
    "id": "INTEGER",
    "contract": "SMARTCONTRACT", # 关联smart_contracts表
    "variable_name": "VARCHAR", # 变量名
    "variable_value": "TEXT", # 变量值
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP"
}

# 日志
log_format = {
    "id": "INTEGER",
    "action": "VARCHAR", # 从user/team/collection/project/contract中选择
    "detail": "TEXT",
    "smart_contract_trigger_log_id":"INTEGER" # 和smart_contract_trigger_logs相关联
}