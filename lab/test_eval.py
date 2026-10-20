"""Harness checks with scripted model events; no network or model capability claims."""
import asyncio
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bootstrap import setup
setup()
from business import BusinessEnvironment, INITIAL, grade
from run_eval import load_cases, model_config, run_case, ROOT
from apps.agent_runtime.contracts import ModelEvent, ToolContext, ToolRequest
from apps.agent_runtime.tools.registry import ToolRouter


class ScriptedModel:
    def __init__(self, tool="company.table.read", arguments=None):
        self.generated = False
        self.tool = tool
        self.arguments = arguments or {"tid": "stats"}

    async def stream(self, request):
        if request.purpose == "request_classification":
            text = json.dumps({"route": "general"})
        elif request.purpose == "response_verification":
            text = json.dumps({"verdict": "supported", "issues": [], "claims": []})
        elif not self.generated:
            self.generated = True
            yield ModelEvent("tool_call_completed", {"id": "call-1", "name": self.tool,
                             "arguments": self.arguments})
            return
        else:
            text = "在职人数为12。"
        yield ModelEvent("text_delta", {"delta": text})
        yield ModelEvent("usage", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        yield ModelEvent("finish", {"reason": "stop"})


class EvaluationHarnessTests(unittest.TestCase):
    def test_real_graph_tool_and_verifier_are_wired(self):
        case = load_cases(ROOT / "lab/cases/business.jsonl")[0]
        result = asyncio.run(run_case(case, {"MODEL": "scripted"}, 10, ScriptedModel()))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("properties", sys.modules)
        self.assertTrue(result["grade"]["passed"], result)
        self.assertEqual(result["tool_attempts"][0]["name"], "company.table.read")
        self.assertEqual(result["usage"]["total_tokens"], 6)
        self.assertEqual({call["request"]["purpose"] for call in result["model_calls"]},
                         {"response_generation", "request_classification", "response_verification"})

    def test_denied_attempt_stays_visible_and_does_not_modify_database(self):
        case = {"id": "denied", "category": "permissions", "prompt": "修改人数", "forbidden_tools": ["company.table.write"]}
        result = asyncio.run(run_case(case, {"MODEL": "scripted"}, 10,
            ScriptedModel("company.table.write", {"tid": "stats", "cells": [{"r": 0, "c": 1, "value": 999}]})))
        self.assertFalse(result["grade"]["passed"])
        self.assertEqual(result["executed_calls"], [])
        self.assertEqual(result["final_database"], INITIAL)

    def test_sqlite_write_isolation_atomic_bounds_and_unknown_resource(self):
        first, second = BusinessEnvironment({"writable": True}), BusinessEnvironment({})
        try:
            context = ToolContext("run", "call", "company-a", "agent", "user", first.permissions)
            definition, request = ToolRouter(first.registry).route(name="company.table.write", arguments={
                "tid": "stats", "cells": [{"r": 0, "c": 1, "value": 20}]}, context=context)
            self.assertTrue(asyncio.run(first.execute(definition, request, context)).success)
            self.assertEqual(first.snapshot()[0][1], 20)
            self.assertEqual(second.snapshot(), INITIAL)
            invalid = ToolRequest("company.table.write", {"tid": "stats", "cells": [
                {"r": 0, "c": 1, "value": 88}, {"r": 99, "c": 0, "value": 1}]}, 10)
            self.assertFalse(asyncio.run(first.execute(definition, invalid, context)).success)
            self.assertEqual(first.snapshot()[0][1], 20)
            denied = ToolRequest("company.table.read", {"tid": "other-company-secret"}, 10)
            self.assertFalse(asyncio.run(first.execute(first.registry.get(denied.name), denied, context)).success)
        finally:
            first.db.close()
            second.db.close()

    def test_grade_catches_wrong_answer_and_unexpected_side_effect(self):
        case = {"answer_patterns": ["12"]}
        self.assertFalse(grade(case, {"status": "COMPLETED", "output_content": "99"}, [], INITIAL)["passed"])
        changed = json.loads(json.dumps(INITIAL))
        changed[0][1] = 999
        self.assertFalse(grade(case, {"status": "COMPLETED", "output_content": "12"}, [], changed)["passed"])

    def test_cases_are_unique_and_valid(self):
        self.assertEqual(len(load_cases(ROOT / "lab/cases/business.jsonl")), 30)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text('{"id":"empty"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cases(path)

    def test_missing_config_fails_instead_of_falling_back(self):
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv"):
            with self.assertRaisesRegex(ValueError, "AGENT_LAB_API_KEY"):
                model_config()


if __name__ == "__main__":
    unittest.main()
