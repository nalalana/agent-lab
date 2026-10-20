"""Adapter experiment: transport observations are not proof of business effects."""

UNKNOWN_CODE = "OPERATION_OUTCOME_UNKNOWN"
UNKNOWN_MESSAGE = (
    "The tool request did not return a confirmed outcome. The operation may or may not "
    "have taken effect. This task has stopped further tool calls. An authorized operator "
    "must reconcile its status before any write is retried."
)


def execution_outcome(*, success, execution_status, may_mutate):
    # Only trusted executor metadata decides this, never text inside tool results.
    if execution_status == "rejected":
        return "not_executed"
    if execution_status == "executed" and success is True:
        return "returned"  # A response arrived; business success is not implied.
    return "unknown" if may_mutate else "read_failed"
