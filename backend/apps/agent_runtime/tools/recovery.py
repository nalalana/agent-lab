"""Trusted recovery guidance; never authorizes or automatically repeats a call."""


def recovery_policy(code: str) -> dict:
    if code == "OPERATION_OUTCOME_UNKNOWN":
        action, instruction = "stop_and_reconcile", "操作结果未确认，可能已生效；停止后续调用，先核验状态，不得自动重试写入。"
    elif code in {"TABLE_WRITE_CONFLICT", "CELL_WRITE_CONFLICT", "ROW_APPEND_CONFLICT"}:
        action, instruction = "reread_and_recheck_intent", "本次操作因版本冲突未执行；先重新读取目标数据并核对用户原始意图和重试限制，条件不再满足时保留他人修改，不得原样重试。"
    elif code == "TABLE_READ_REQUIRED":
        action, instruction = "read_before_write", "先调用 company.table.read 读取目标表，再按用户原始授权和条件决定是否提交修改。"
    elif code == "TOOL_INVALID_ARGUMENTS":
        action, instruction = "correct_arguments", "核对用户原始请求与工具参数定义；仅在授权不变且参数明确时更正参数，不得猜测缺失信息或扩大权限。"
    elif code in {"TOOL_DENIED", "AUTH_DENIED", "RISK_REJECTED"}:
        action, instruction = "explain_denial", "说明实际拒绝原因，不得重复尝试被拒绝的操作、提升权限或绕过限制。"
    else:
        action, instruction = "stop_and_explain", "不要重复调用该工具，请根据错误直接向用户说明。"
    return {"action": action, "instruction": instruction, "automatic_retry_allowed": False}
