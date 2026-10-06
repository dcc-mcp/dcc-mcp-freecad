"""Allowlisted public CI evidence; excludes native diagnostics and host paths."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_value(value):
    if isinstance(value, dict):
        return {
            key: public_value(item)
            for key, item in value.items()
            if key not in {"stdout", "stderr", "url", "executable", "module_directory"}
        }
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("/", "http://", "https://")) or re.match(r"^[A-Za-z]:[\\/]", value):
            return "<owned-file-or-endpoint>"
        return value
    return value


class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.stage = "setup"
        self.events = 0

    def record(self, direction, logical, payload):
        self.stage = logical
        self.events += 1
        assert self.events <= 256, "Unexpected logical evidence volume"
        record = {
            "event": self.events,
            "direction": direction,
            "logical": logical,
            "payload": public_value(payload),
        }
        with (self.directory / "mcp-events.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")

    def report(self, value):
        (self.directory / "report.json").write_text(
            json.dumps(public_value(value), indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
