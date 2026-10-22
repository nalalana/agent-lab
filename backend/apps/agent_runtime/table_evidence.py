"""Derive table observations from one trusted run's chronological receipts."""
import json


def table_observations(results: list[dict]) -> dict[int, dict]:
    later, observations = {}, {}
    for index in range(len(results) - 1, -1, -1):
        result = results[index]
        name = result.get("name")
        if name not in {"company.table.read", "company.table.write", "company.table.clear_cells"}:
            continue
        arguments = result.get("arguments")
        tid = arguments.get("tid") if isinstance(arguments, dict) else None
        if not isinstance(tid, str) or not tid.strip():
            continue
        tid = tid.strip()
        invalidated_by = later.get(tid)
        # Even a failed/invalid read or mutation attempt blocks fallback to an older read.
        later[tid] = f"tool:{result.get('tool_call_id') or index}"
        if (name != "company.table.read" or result.get("success") is not True or result.get("error_code")
                or result.get("execution_status", "returned") != "returned"
                or result.get("business_outcome") in {"not_attempted", "rejected", "conflict"}):
            continue
        try:
            payload = json.loads(result["content"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not isinstance(payload, dict) or payload.get("tid") != tid
                or payload.get("tool_name", name) != name or payload.get("success") is False
                or not isinstance(payload.get("data", payload.get("rows")), (dict, list))):
            continue
        data = payload.get("data")
        version = data.get("table_version") if isinstance(data, dict) else None
        # Legacy readable facts are retained, but write preconditions require an explicit producer and revision.
        write_version = version if (payload.get("tool_name") == name and type(version) is int and version >= 0) else None
        observations[index] = {"tid": tid, "payload": payload, "write_version": write_version,
            "status": "historical" if invalidated_by else "observed", "invalidated_by": invalidated_by}
    return observations
