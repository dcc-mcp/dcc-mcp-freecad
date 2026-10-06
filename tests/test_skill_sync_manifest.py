# -*- coding: utf-8 -*-
"""`.kimi/skills` 与 Monica workspace skill registry 的同步清单单测。

PR #36 合并进仓库后 runtime 仍加载旧版，因为 runtime 读的是
registry 而不是仓库。`.kimi/skills` 是**带单测的源树**（tests/ 按路径加载它），
registry 是**runtime 源**；两者靠 scripts/sync_monica_skills.py 保持一致。

这里守住清单本身：
  - 清单里的每个目录必须真实存在且带 SKILL.md
  - `.kimi/skills` 下的每个 skill 目录都必须在清单里（漏登记 = 不同步）
  - skill id 必须是 UUID，避免手写时粘错
"""

from __future__ import annotations

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
