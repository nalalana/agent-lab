from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured

from config.agsx_runtime_env import load_agsx_runtime_env


class AgsxRuntimeEnvironmentTests(TestCase):
    def test_loads_only_runtime_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                'AGSX_ENABLED="true"\n'
                'E2B_API_KEY="runtime-test-key"\n'
                'AGENT_RUNTIME_ARTIFACT_COS_PREFIX="agent-runtime/v1"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_agsx_runtime_env(str(path))
                self.assertEqual(os.environ["AGSX_ENABLED"], "true")
                self.assertEqual(os.environ["E2B_API_KEY"], "runtime-test-key")
            self.assertEqual(loaded, path)

    def test_rejects_deployment_secret_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                "AGSX_ENABLED=true\nAGSX_REGISTRY_PASSWORD=must-not-load\n",
                encoding="utf-8",
            )
            with self.assertRaises(ImproperlyConfigured):
                load_agsx_runtime_env(str(path))

    def test_rejects_relative_and_symlink_paths(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            load_agsx_runtime_env("runtime.env")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "real.env"
            link = Path(directory) / "runtime.env"
            target.write_text("AGSX_ENABLED=true\n", encoding="utf-8")
            link.symlink_to(target)
            with self.assertRaises(ImproperlyConfigured):
                load_agsx_runtime_env(str(link))
