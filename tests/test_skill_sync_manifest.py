# -*- coding: utf-8 -*-
"""`.kimi/skills` 与 Monica workspace skill registry 的同步清单单测。

PR #36 合并进仓库后 runtime 仍加载旧版，因为 runtime 读的是
registry 而不是仓库。`.kimi/skills` 是**带单测的源树**（tests/ 按路径加载它），
registry 是**runtime 源**；两者靠 scripts/sync_monica_skills.py 保持一致。

这里守住清单本身：
  - 清单里的每个目录必须真实存在且带 SKILL.md
  - `.kimi/skills` 下的每个 skill 目录都必须在清单里（漏登记 = 不同步）
  - skill id 必须是 UUID，避免手写时粘错

以及比对/同步的核心逻辑（local_snapshot / registry_snapshot / diff / push）：
  - 行尾归一化，让结论由内容决定而不是由 autocrlf 决定
  - 跳过 __pycache__/点文件，别把编译产物推给 runtime
  - diff 的三个分支 + push 的删除分支
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / ".kimi" / "skills"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_monica_skills.py"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _load_manifest():
    """按路径加载同步脚本，读出 SKILLS 清单。"""
    import importlib.util

    if not SYNC_SCRIPT.exists():
        pytest.skip(f"同步脚本不存在：{SYNC_SCRIPT}")
    if not SKILLS_ROOT.is_dir():
        pytest.skip(f".kimi/skills 未挂载：{SKILLS_ROOT}")
    spec = importlib.util.spec_from_file_location("sync_monica_skills", SYNC_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


manifest = _load_manifest()


def test_skill_tree_present():
    """至少登记了一个 skill，且源树就在仓库里。"""
    assert manifest.SKILLS, "SKILLS 清单为空，同步脚本无事可做"
    assert SKILLS_ROOT.is_dir()


def test_manifest_ids_are_uuids():
    for skill_id, rel in manifest.SKILLS.items():
        assert UUID_RE.match(skill_id), f"skill id 不是 UUID：{skill_id} -> {rel}"
        # 顺带确认 UUID 可解析，挡住"看起来像 UUID"的手抄错误
        assert str(uuid.UUID(skill_id)) == skill_id.lower()


def test_manifest_dirs_exist_with_body():
    for _skill_id, rel in manifest.SKILLS.items():
        root = REPO_ROOT / rel
        assert root.is_dir(), f"清单指向了不存在的目录：{rel}"
        assert (root / manifest.SKILL_BODY).is_file(), f"{rel} 缺少 {manifest.SKILL_BODY}"


def test_every_skill_dir_is_registered():
    """漏登记的 skill 目录不会被同步 —— 正是这次的失效模式。"""
    on_disk = sorted(p.name for p in SKILLS_ROOT.iterdir() if p.is_dir())
    registered = sorted(Path(rel).name for rel in manifest.SKILLS.values())
    assert on_disk == registered, (
        f".kimi/skills 与同步清单不一致：磁盘 {on_disk} vs 清单 {registered}"
    )


def test_manifest_paths_live_under_skills_root():
    """清单路径必须落在 .kimi/skills 下：同步脚本会整棵推给 registry，
    指到别处就会把仓库里的无关文件一起推上去。"""
    root = SKILLS_ROOT.resolve()
    for _skill_id, rel in manifest.SKILLS.items():
        resolved = (REPO_ROOT / rel).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            pytest.fail(f"清单路径应在 .kimi/skills 下：{rel}")


# --------------------------------------------------------------------------
# 比对与同步核心逻辑：local_snapshot / diff / registry_snapshot / push
# --------------------------------------------------------------------------


@pytest.fixture()
def sync_mod():
    """按路径加载同步脚本（与 _load_manifest 同一机制）。"""
    return _load_manifest()


def _skill_payload(files=None, content="body"):
    """构造 `monica skill get --with-content --output json` 的返回形状。"""
    payload = {"content": content}
    if files is not None:
        payload["files"] = files
    return json.dumps(payload)


def test_local_snapshot_normalizes_crlf(tmp_path, sync_mod):
    """CRLF 检出与 LF 检出必须判同一份内容，否则 autocrlf 决定结论。"""
    (tmp_path / "a.md").write_bytes(b"line\r\nline\r\n")
    snap = sync_mod.local_snapshot(tmp_path)
    assert snap == {"a.md": b"line\nline\n"}


def test_local_snapshot_skips_pycache(tmp_path, sync_mod):
    """pytest 按路径 import skill 脚本会留下 __pycache__，不能算进快照。"""
    (tmp_path / "SKILL.md").write_bytes(b"body")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_bytes(b"x = 1")
    cache = tmp_path / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "tool.cpython-312.pyc").write_bytes(b"\x00binary")
    (tmp_path / ".hidden").write_bytes(b"secret")

    snap = sync_mod.local_snapshot(tmp_path)
    assert set(snap) == {"SKILL.md", "scripts/tool.py"}


def test_diff_all_three_branches(sync_mod):
    """未同步 / 仓库缺文件 / 内容不一致 三个分支都要能报出来。"""
    local = {"keep.md": b"same", "only_local.md": b"new", "changed.md": b"local"}
    remote = {"keep.md": b"same", "only_registry.md": b"old", "changed.md": b"remote"}

    problems = sync_mod.diff(local, remote)
    joined = "\n".join(problems)
    assert any("only_local.md" in p and "只在仓库里" in p for p in problems)
    assert any("only_registry.md" in p and "只在注册表里" in p for p in problems)
    assert any("changed.md" in p and "内容不一致" in p for p in problems)
    assert "keep.md" not in joined


def test_diff_identical_is_empty(sync_mod):
    assert sync_mod.diff({"a.md": b"x"}, {"a.md": b"x"}) == []


def test_registry_snapshot_nonzero_exit(sync_mod, monkeypatch, capsys):
    monkeypatch.setattr(sync_mod, "run", lambda cmd: (1, "", "boom"))
    assert sync_mod.registry_snapshot("some-id") is None
    assert "boom" in capsys.readouterr().err


def test_registry_snapshot_non_json(sync_mod, monkeypatch, capsys):
    monkeypatch.setattr(sync_mod, "run", lambda cmd: (0, "not json", ""))
    assert sync_mod.registry_snapshot("some-id") is None
    assert "返回非 JSON" in capsys.readouterr().err


def test_registry_snapshot_shapes_and_normalizes(sync_mod, monkeypatch):
    files = [{"path": "scripts/a.py", "content": "print(1)\r\n"}]
    monkeypatch.setattr(
        sync_mod, "run", lambda cmd: (0, _skill_payload(files, content="head\r\n"), "")
    )
    snap = sync_mod.registry_snapshot("some-id")
    assert snap == {"SKILL.md": b"head\n", "scripts/a.py": b"print(1)\n"}


def test_push_deletes_files_missing_from_repo(tmp_path, sync_mod, monkeypatch):
    """删除型漂移必须能修：push 要真的删，而不是只 upsert。"""
    (tmp_path / "SKILL.md").write_bytes(b"body")
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[1:4] == ["skill", "files", "list"]:
            return (0, json.dumps([{"path": "gone.py", "id": "file-1"}]), "")
        return (0, "", "")

    monkeypatch.setattr(sync_mod, "run", fake_run)

    assert sync_mod.push("skill-1", tmp_path, {"SKILL.md": b"body"}) is True
    assert ["monica", "skill", "files", "delete", "skill-1", "file-1"] in calls


def test_push_reports_write_failure(tmp_path, sync_mod, monkeypatch):
    (tmp_path / "SKILL.md").write_bytes(b"body")

    def fake_run(cmd):
        if cmd[1:4] == ["skill", "files", "list"]:
            return (0, "[]", "")
        return (1, "", "denied")

    monkeypatch.setattr(sync_mod, "run", fake_run)
    assert sync_mod.push("skill-1", tmp_path, {"SKILL.md": b"body"}) is False
