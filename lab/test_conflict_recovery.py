import asyncio
from dataclasses import replace
import json
import unittest
from run_conflict_recovery import ConflictEnvironment
from apps.agent_runtime.contracts import ToolContext, ToolRequest


class ConflictRecoveryTests(unittest.TestCase):
    def test_conflict_requires_reread_before_authorized_write(self):
        env = ConflictEnvironment({"writable": True, "mode": "once"})
        context = ToolContext("r", "t", "company-a", "a", "u", env.permissions)
        def execute(name, args, version=None):
            return asyncio.run(env.execute(env.registry.get(name), ToolRequest(name, args, 5),
                replace(context, expected_table_version=version)))
        try:
            args = {"tid": "stats", "cells": [{"r": 0, "c": 1, "value": 15}]}
            rejected = execute("company.table.write", args)
            self.assertEqual(rejected.error_code, "TABLE_READ_REQUIRED")
            self.assertEqual((rejected.execution_status, rejected.business_outcome), ("not_dispatched", "not_attempted"))
            self.assertFalse(rejected.success)
            self.assertEqual((env.agent_commits, env.snapshot()[0][1]), (0, 12))
            receipt = json.loads(execute("company.table.read", {"tid": "stats"}).content)
            version = receipt["data"]["table_version"]
            conflict = execute("company.table.write", args, version)
            self.assertEqual(conflict.error_code, "TABLE_WRITE_CONFLICT")
            self.assertEqual((conflict.execution_status, conflict.business_outcome), ("returned", "conflict"))
            self.assertFalse(conflict.success)
            self.assertEqual(env.snapshot()[0][1], 20)
            self.assertEqual(execute("company.table.write", args, version).error_code, "TABLE_WRITE_CONFLICT")
            self.assertEqual(env.agent_commits, 0)
            version = json.loads(execute("company.table.read", {"tid": "stats"}).content)["data"]["table_version"]
            self.assertTrue(execute("company.table.write", args, version).success)
            self.assertEqual(env.snapshot()[0][1], 15)
            self.assertEqual(env.agent_commits, 1)
        finally:
            env.db.close()

    def test_repeated_clear_conflicts_never_clear_external_value(self):
        env = ConflictEnvironment({"writable": True, "mode": "always"})
        try:
            for version in (0, 1):
                context = ToolContext("r", "t", "company-a", "a", "u", env.permissions, version)
                request = ToolRequest("company.table.clear_cells", {"tid": "stats", "cells": [{"r": 0, "c": 1}]}, 5)
                result = asyncio.run(env.execute(env.registry.get(request.name), request, context))
                self.assertEqual(result.error_code, "TABLE_WRITE_CONFLICT")
                self.assertEqual((result.execution_status, result.business_outcome), ("returned", "conflict"))
                self.assertFalse(result.success)
            self.assertEqual((env.conflicts, env.agent_commits, env.snapshot()[0][1]), (2, 0, 21))
        finally:
            env.db.close()


if __name__ == "__main__":
    unittest.main()
