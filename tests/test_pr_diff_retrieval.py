# -*- coding: utf-8 -*-
"""fetch_pr_diff.py 的凭据脱敏与浅克隆回退单测。

对应 exact-head 审核（2026-10-06）的两条 finding：
  - 阻断项：浅克隆回退静默返回空 diff（rc 未检查 + 三点式需要 merge base）
  - P2：token 拼进 clone URL，git 输出可能带出凭据
  - P3：http_get 漏 catch timeout / JSON 解析错误
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SKILL_SCRIPTS = (
    Path(__file__).resolve().parent.parent / ".kimi" / "skills" / "code-review" / "scripts"
)


def _load_module():

    script = SKILL_SCRIPTS / "fetch_pr_diff.py"
    if not script.exists():
        pytest.skip(f"code-review skill 未挂载，跳过：{script}")
    spec = importlib.util.spec_from_file_location("fetch_pr_diff", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fpd = _load_module()


def test_redact_masks_token():
    text = "fatal: could not read https://x-token:ghp_SUPERSECRET@github.com/o/r.git"
    out = fpd.redact(text, "ghp_SUPERSECRET")
    assert "ghp_SUPERSECRET" not in out
    assert "***" in out


def test_redact_masks_url_credentials_without_known_secret():
    """即使没拿到 token 对象，也要抹掉 URL 里的 user:pass@。"""
    out = fpd.redact("error: https://user:pass@github.com/o/r.git", None)
    assert "pass" not in out
    assert "https://***@github.com" in out


def test_redact_leaves_clean_text_alone():
    text = "fatal: repository not found"
    assert fpd.redact(text, "tok") == text


def test_redact_handles_empty():
    assert fpd.redact("", "tok") == ""


def test_token_is_not_embedded_in_clone_url():
    """凭据必须走 http.extraHeader，不能拼进 clone URL。"""
    src = (SKILL_SCRIPTS / "fetch_pr_diff.py").read_text(encoding="utf-8")
    assert "http.extraHeader=AUTHORIZATION: Bearer" in src
    assert "http.extraHeader=PRIVATE-TOKEN: " in src
    # 旧的 `https://x-token:{token}@` 拼接必须已移除
    assert "https://x-token:{token}@" not in src
    assert "https://oauth2:{token}@" not in src


def test_shallow_clone_diff_uses_two_dot_form():
    """三点式 `base...head` 需要 merge base，depth=50 常常没有 → 用两点式。"""
    src = (SKILL_SCRIPTS / "fetch_pr_diff.py").read_text(encoding="utf-8")
    assert 'f"{base_sha}...{head_sha}"' not in src
    assert '"git", "diff", base_sha, head_sha' in src


def test_fetch_return_codes_are_checked():
    """两次 fetch 的返回码都必须检查 —— 否则失败后 diff 静默返回空串。"""
    src = (SKILL_SCRIPTS / "fetch_pr_diff.py").read_text(encoding="utf-8")
    assert "rf.returncode != 0" in src


def test_empty_diff_treated_as_failure():
    """空 diff 必须报告为获取失败，不能当作「这个 PR 没有改动」。"""
    src = (SKILL_SCRIPTS / "fetch_pr_diff.py").read_text(encoding="utf-8")
    assert "r2.returncode == 0 and r2.stdout.strip()" in src
    assert "视为获取失败而非空 PR" in src


def test_http_get_catches_timeouts_and_json_errors():
    src = (SKILL_SCRIPTS / "fetch_pr_diff.py").read_text(encoding="utf-8")
    assert "except (TimeoutError, OSError)" in src
    assert "except json.JSONDecodeError" in src
