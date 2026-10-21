"""Storage execution uncertainty must not be treated as a safe-to-retry failure."""


class UnconfirmedWriteError(RuntimeError):
    code = "OPERATION_OUTCOME_UNKNOWN"

    def __init__(self):
        super().__init__("写入结果未确认，可能已生效；请先核验状态，不要自动重试。")
