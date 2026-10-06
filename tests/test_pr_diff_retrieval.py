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


# ── Python 3.7 兼容门禁 ──────────────────────────────────────────────────────
# 组织红线（PIP-2519）：至 2026-12-31 整个 dcc-mcp 组织须保持 py3.7 兼容。
# pyproject 的 requires-python 是 ">=3.7"，CI 也真跑 py3.7 lane。
#
# 曾踩过的具体回归：`def http_get(...) -> dict | str`。
# PEP 604 的 `X | Y` 在 ≤3.9 求值时抛 TypeError，而注解在 import 时就求值，
# 于是 pytest 在 **collection 阶段** 直接 Interrupted —— 五个 lane 全红，
# 本地 py3.12 却全绿，属于只在旧解释器上暴露的一类。


def _py37_syntax_errors(path):
    """返回该文件在 py3.7 语法下的错误列表；无法判定时返回 None。

    `ast.parse(feature_version=...)` 是 **Python 3.8 才加入** 的参数，
    在 3.7 上调它会抛 TypeError —— 用它做 3.7 门禁时，门禁自己先在 3.7 上炸了。
    3.7 上没有等价的语法门，此时降级为"仅解析 + PEP 604 检查"（见另一条用例），
    不伪造结论。
    """
    import ast
    import inspect

    if "feature_version" not in inspect.signature(ast.parse).parameters:
        return None

    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 7))
        return []
    except SyntaxError as exc:
        return [f"{path.name}:L{exc.lineno} {exc.msg}"]


@pytest.mark.parametrize("name", ["fetch_pr_diff.py", "collect_pr_context.py", "analyze.py"])
def test_skill_scripts_parse_as_python_37(name):
    errors = _py37_syntax_errors(SKILL_SCRIPTS / name)
    if errors is None:
        # 运行在 3.7 上：该解释器本就只接受 3.7 语法，普通 parse 即是 3.7 门禁
        import ast

        ast.parse((SKILL_SCRIPTS / name).read_text(encoding="utf-8"))
        return
    assert errors == []


def test_skill_scripts_avoid_pep604_unions():
    """不做 `from __future__ import annotations` 的脚本不得用 `X | Y`。

    有 future import 时注解是字符串、不会求值，PEP 604 无害；
    没有该 import 时 `X | Y` 在 import 阶段就炸。
    这里按"是否声明 future import"分别判定，避免一刀切。
    """
    import ast

    for path in sorted(SKILL_SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_future = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "__future__"
            and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        )
        unions = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
        ]
        if not has_future:
            assert not unions, (
                f"{path.name} 使用 PEP 604 联合类型（L{unions}）但未声明 "
                f"`from __future__ import annotations`，在 py<=3.9 上会 "
                f"于 import 时抛 TypeError"
            )


def test_skill_scripts_import_on_current_interpreter():
    """三个脚本在当前解释器下都能被 import。

    捕捉语法之外的问题：缺失的 import、模块级副作用、只在 import 时才炸的注解求值。
    """
    import importlib.util

    for path in sorted(SKILL_SCRIPTS.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"_probe_{path.stem}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)


def test_project_requires_python_37():
    """若放宽 requires-python，上面几条 py3.7 门禁也要跟着重新评估。"""
    text = (
        Path(__file__)
        .resolve()
        .parent.parent.joinpath("pyproject.toml")
        .read_text(encoding="utf-8")
    )
    assert ">=3.7" in text
