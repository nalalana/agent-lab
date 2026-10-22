"""Fingerprint executable lab inputs and the installed Python environment."""
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re
import sys


def source_identity(root):
    root = Path(root)
    excluded = {"datasets", "results", ".venv", "venv", "__pycache__", ".git"}
    prompts = root / "lab/prompts"
    files = []
    for folder in (root / "backend/apps/agent_runtime", root / "lab"):
        for parent, directories, names in os.walk(folder):
            directories[:] = [name for name in directories if name not in excluded]
            for name in names:
                path = Path(parent) / name
                if path.suffix == ".py" or path.is_relative_to(prompts):
                    files.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    encoded = json.dumps(sorted(files), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Snapshot all installed distributions so transitive dependency changes also stop resume.
    dependencies = dict(sorted((re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(), dist.version)
                               for dist in distributions() if dist.metadata["Name"]))
    return {"source_sha256": hashlib.sha256(encoded).hexdigest(), "python": sys.version,
            "dependencies": dependencies}
