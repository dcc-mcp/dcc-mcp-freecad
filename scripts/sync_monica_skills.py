#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Push the test-backed skill trees under `.kimi/skills` into the Monica
workspace skill registry, then verify the registry matches byte for byte.

Why this exists
---------------
The registry, not the repository, is what an agent run actually loads: Monica
materialises the workspace skill into the task's `agents_ide_dir` at startup.
A change that lands only in the repository therefore looks done in review while
every later run keeps loading the previous version. That is exactly what
happened to the `code-review` classifier: the container-comment
classification, verdict ledger, and identity check were merged and green, and
still no run ever saw them.

The repository copy is not redundant, though. `tests/` loads the scripts from
there by path and skips silently when the tree is absent, so deleting it would
turn real coverage into green no-ops. The repository copy is the test-backed
source tree; the registry is the runtime source. This script keeps the two from
drifting.

Usage
-----
    python scripts/sync_monica_skills.py            # push, then verify
    python scripts/sync_monica_skills.py --check    # verify only, no writes

Exit codes
----------
    0   registry matches the repository for every skill
    1   drift remains after the push, or a `monica` call failed
    2   the manifest names a directory that does not exist

Adding a skill
--------------
Add it to `SKILLS` below and to `tests/test_skill_sync_manifest.py`, which
fails when a directory under `.kimi/skills` is missing from the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Monica workspace skill id -> the test-backed tree in this repository.
# Resolve an id with `monica skill list --output json` before adding one.
SKILLS: Dict[str, str] = {
    "5d25ca4d-a4d8-40bb-a139-745507a7fc6d": ".kimi/skills/code-review",
    "5f61d9c7-6c0c-4935-89ae-8fc7706dbf57": ".kimi/skills/monica-github-autopilot-ops",
}

# The skill body is not a "file" on the registry; it is the skill's own content.
SKILL_BODY = "SKILL.md"


def _force_utf8_streams() -> None:
    """Windows/zh-CN hosts default to cp936; the report below is not ASCII."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - wrapped or non-TTY streams have no reconfigure
            pass


def run(cmd: List[str]) -> Tuple[int, str, str]:
    """Run without a shell: paths and ids here are never shell-quoted."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _normalize(data: bytes) -> bytes:
    """Compare and push LF-normalized bytes.

    `core.autocrlf` decides the checkout's line endings, not the content, so a
    raw byte compare flips from "0 drift" to "everything drifted" depending on
    which host last ran the push. Normalizing on both sides makes the verdict a
    property of the content and keeps push and check on one code path.
    """
    return data.replace(b"\r\n", b"\n")


def _is_tracked(path: Path, root: Path) -> bool:
    """Skip build artefacts and dotfiles that must never reach the registry.

    `tests/` imports the skill scripts by path, so a pytest run leaves
    `__pycache__/*.pyc` behind; without this filter the next `--check` reports
    three bogus drifts and the following push uploads compiled binaries into
    what an agent run actually mounts.
    """
    parts = path.relative_to(root).parts
    return not any(
        part in {"__pycache__", ".git", ".hg", ".svn"}
        or part.endswith(".pyc")
        or (part.startswith(".") and part != ".")
        for part in parts
    )


def local_snapshot(root: Path) -> Dict[str, bytes]:
    """Every tracked file in the skill tree, keyed by its registry path."""
    out: Dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and _is_tracked(path, root):
            out[path.relative_to(root).as_posix()] = _normalize(path.read_bytes())
    return out


def registry_snapshot(skill_id: str) -> Optional[Dict[str, bytes]]:
    """Read the bytes an agent run would actually mount."""
    code, out, err = run(["monica", "skill", "get", skill_id, "--with-content", "--output", "json"])
    if code != 0:
        print(f"  monica skill get {skill_id} 失败（exit {code}）: {err.strip()}", file=sys.stderr)
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"  monica skill get {skill_id} 返回非 JSON: {exc}", file=sys.stderr)
        return None
    snapshot = {
        SKILL_BODY: _normalize((payload.get("content") or "").encode("utf-8")),
    }
    for entry in payload.get("files") or []:
        snapshot[str(entry["path"])] = _normalize((entry.get("content") or "").encode("utf-8"))
    return snapshot


def registry_file_ids(skill_id: str) -> Dict[str, str]:
    """Registry path -> file id, needed to delete a file the repo dropped."""
    code, out, err = run(["monica", "skill", "files", "list", skill_id, "--output", "json"])
    if code != 0:
        print(
            f"  monica skill files list {skill_id} 失败（exit {code}）: {err.strip()}",
            file=sys.stderr,
        )
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        print(f"  monica skill files list {skill_id} 返回非 JSON: {exc}", file=sys.stderr)
        return {}
    entries = payload if isinstance(payload, list) else (payload.get("files") or [])
    return {str(e["path"]): str(e["id"]) for e in entries if "path" in e and "id" in e}


def push(skill_id: str, root: Path, snapshot: Dict[str, bytes]) -> bool:
    """Upsert every local file, then delete registry files the repo dropped."""
    ok = True
    for rel in sorted(snapshot):
        if rel == SKILL_BODY:
            cmd = ["monica", "skill", "update", skill_id, "--content-file", str(root / rel)]
        else:
            cmd = [
                "monica",
                "skill",
                "files",
                "upsert",
                skill_id,
                "--path",
                rel,
                "--content-file",
                str(root / rel),
            ]
        code, _out, err = run(cmd)
        if code != 0:
            ok = False
            print(f"  写入失败 {rel}（exit {code}）: {err.strip()}", file=sys.stderr)

    remote_ids = registry_file_ids(skill_id)
    for rel in sorted(set(remote_ids) - set(snapshot)):
        if rel == SKILL_BODY:
            continue
        code, _out, err = run(["monica", "skill", "files", "delete", skill_id, remote_ids[rel]])
        if code != 0:
            ok = False
            print(f"  删除失败 {rel}（exit {code}）: {err.strip()}", file=sys.stderr)
        else:
            print(f"  已删除注册表里不存在于仓库的文件 {rel}")
    return ok


def diff(local: Dict[str, bytes], remote: Dict[str, bytes]) -> List[str]:
    problems: List[str] = []
    for rel in sorted(set(local) | set(remote)):
        if rel not in remote:
            problems.append(f"{rel}: 只在仓库里（未同步）")
        elif rel not in local:
            problems.append(f"{rel}: 只在注册表里（仓库缺文件）")
        elif local[rel] != remote[rel]:
            problems.append(
                f"{rel}: 内容不一致 仓库 {len(local[rel])}B/{_sha(local[rel])} "
                f"≠ 注册表 {len(remote[rel])}B/{_sha(remote[rel])}"
            )
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验，不写入注册表",
    )
    args = parser.parse_args(argv)

    missing = [rel for rel in SKILLS.values() if not (REPO_ROOT / rel).is_dir()]
    if missing:
        print("SKILLS 指向了不存在的目录，先修正清单：", file=sys.stderr)
        for rel in missing:
            print(f"  - {rel}", file=sys.stderr)
        return 2

    failed = False
    for skill_id, rel in sorted(SKILLS.items(), key=lambda item: item[1]):
        root = REPO_ROOT / rel
        print(f"== {rel} -> {skill_id}")
        local = local_snapshot(root)

        if not args.check:
            if not push(skill_id, root, local):
                failed = True
                continue

        remote = registry_snapshot(skill_id)
        if remote is None:
            failed = True
            continue

        problems = diff(local, remote)
        if problems:
            failed = True
            for problem in problems:
                print(f"  DRIFT {problem}")
        else:
            print(f"  OK {len(local)} 个文件与注册表逐字节一致")

    if failed:
        print(
            "\n注册表与仓库不一致：运行 python scripts/sync_monica_skills.py 重新同步"
            "（新增/修改/删除的文件都会同步，删除需要 monica CLI 与工作区权限）。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
