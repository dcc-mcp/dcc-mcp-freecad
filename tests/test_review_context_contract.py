# -*- coding: utf-8 -*-
"""Review Context Contract 分类器 / 容器评论 / 恒等式 / 裁决台账 单测。

对应 PIP-4283 验收标准 3（分类器带单测）、4（container 标记）、5（台账）、6（恒等式机械校验）。

覆盖的契约锚点（`code-review/references/review-context.md`，2026-09-22 hallong 决策）：
  - 文档点名的每个 bot login 必须落到文档声明的类别
  - 容器评论（汇总型/入口型）计入 out_of_scope 且计入 S2 总条数
  - `总条数 = 五态之和` 与 `total = 分类之和` 两条恒等式
  - self_echo 覆盖我们全部对外评论身份，且必须压过 ai_reviewer
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILL_SCRIPTS = (
    Path(__file__).resolve().parent.parent / ".kimi" / "skills" / "code-review" / "scripts"
)


def _load_module():
    """按路径加载采集器。脚本不在包里，也没有 .py 后缀之外的依赖。"""
    import importlib.util

    script = SKILL_SCRIPTS / "collect_pr_context.py"
    if not script.exists():
        pytest.skip(f"code-review skill 未挂载，跳过：{script}")
    spec = importlib.util.spec_from_file_location("collect_pr_context", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 不注册进 sys.modules：避免 _force_utf8_streams 影响其他测试的 stdout 编码
    spec.loader.exec_module(mod)
    return mod


cpc = _load_module()


# ── 验收标准 3：契约文档点名的 bot login 必须落到文档声明的类别 ──────────────


@pytest.mark.parametrize(
    "login,expected",
    [
        # review-context.md:48 点名的 ai_reviewer
        ("coderabbitai[bot]", "ai_reviewer"),
        ("github-advanced-security[bot]", "ai_reviewer"),
        ("copilot", "ai_reviewer"),
        ("chatgpt-codex-connector[bot]", "ai_reviewer"),
        ("greptile", "ai_reviewer"),
        ("codex", "ai_reviewer"),
        # PIP-4283 P2 补齐的安全/依赖扫描类（产出可裁决 finding，不是 CI 信号）
        ("snyk-bot", "ai_reviewer"),
        ("semgrep[bot]", "ai_reviewer"),
        ("trivy", "ai_reviewer"),
        ("osv-scanner[bot]", "ai_reviewer"),
        ("stepsecurity-bot", "ai_reviewer"),
        ("ellipsis-dev[bot]", "ai_reviewer"),
        ("graphite-app[bot]", "ai_reviewer"),
        ("codspeed-hq[bot]", "ai_reviewer"),
        # 其他既有 ai_reviewer
        ("cursor[bot]", "ai_reviewer"),
        ("sourcery-ai[bot]", "ai_reviewer"),
        ("deepsource", "ai_reviewer"),
        ("codiumai", "ai_reviewer"),
        ("bugbot", "ai_reviewer"),
        ("gemini-code-assist[bot]", "ai_reviewer"),
        ("pr-agent", "ai_reviewer"),
        ("qodo-merge", "ai_reviewer"),
        ("corgea", "ai_reviewer"),
        ("codeant-ai", "ai_reviewer"),
        # review-context.md:49 的 ci_bot
        ("codecov[bot]", "ci_bot"),
        ("coveralls", "ci_bot"),
        ("sonarcloud[bot]", "ci_bot"),
        ("dependabot[bot]", "ci_bot"),
        ("renovate[bot]", "ci_bot"),
        ("github-actions[bot]", "ci_bot"),
        ("pre-commit-ci[bot]", "ci_bot"),
    ],
)
def test_contract_named_bots_classify_as_declared(login, expected):
    assert cpc.classify_author(login, True) == expected


@pytest.mark.parametrize(
    "login",
    [
        "monica",
        "pipelineDev",
        "loonghao-agent",
        "loonghao",
        "hallong",
        "xiaoli",
        "qianqian",
        "dcc-mcp-agent-plugins",
        "review-ai-bot",
    ],
)
def test_self_echo_covers_our_own_identities(login):
    """我们自己的对外评论身份必须判 self_echo，且压过 ai_reviewer。

    AI_REVIEWER_PATTERNS 含 r"claude"/r"anthropic"：若 self_echo 判定不在前面，
    我们自己的 claude 系 agent 评论会被当成外部 AI reviewer 拿去裁决 = 自证循环。
    """
    assert cpc.classify_author(login, False, "Please fix the resource leak") == "self_echo"


def test_self_echo_precedence_over_ai_reviewer():
    """顺序即优先级：登录名同时命中两表时 self_echo 胜出。"""
    assert cpc.classify_author("loonghao-claude-agent", False, "nit") == "self_echo"
    # 真正的外部 claude 仍应判 ai_reviewer
    assert cpc.classify_author("claude[bot]", True, "nit") == "ai_reviewer"


def test_unknown_human_and_bot_fallback():
    assert cpc.classify_author("octocat", False, "looks good") == "human"
    assert cpc.classify_author("some-random-bot", True, "") == "ci_bot"
    assert cpc.classify_author("", True) == "ci_bot"
    assert cpc.classify_author("", False) == "human"


def test_chatgpt_codex_connector_not_relying_on_substring():
    """PIP-4283 P2：chatgpt-codex-connector 必须显式命中，不能只靠 r"codex" 子串。"""
    assert "chatgpt-codex-connector" in cpc.AI_REVIEWER_PATTERNS
    assert cpc.classify_author("chatgpt-codex-connector[bot]", True) == "ai_reviewer"


def test_github_advanced_security_is_not_demoted_to_ci_bot():
    """PIP-4283 P1：安全 bot 降格成 ci_bot 会让 CodeQL finding 永远不被逐条裁决。"""
    assert cpc.classify_author("github-advanced-security[bot]", True) == "ai_reviewer"


# ── 验收标准 4：容器评论 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "Actionable comments posted: 3",
        "Actionable comments posted: 0",
        "5 issues found",
        "This PR was reviewed",
        "No issues found!",
        "No issues found",
        "0 issues found",
        "No new issues to report",
        "No blocking issues",
        "All checks passed",
        "LGTM",
    ],
)
def test_summary_container_comments(body):
    assert cpc.classify_author("coderabbitai[bot]", True, body) == "container"


@pytest.mark.parametrize(
    "body",
    [
        "View reviewed changes",
        "See the walkthrough",
        "change-stack",
        "View full report",
    ],
)
def test_navigation_container_comments(body):
    assert cpc.classify_author("coderabbitai[bot]", True, body) == "container"


@pytest.mark.parametrize(
    "body",
    [
        "This function leaks the file handle when an exception is raised before close() is called.",
        "Consider extracting this into a helper for readability.",
        "Off-by-one: the loop writes one element past the buffer end.",
        "Missing lock around this shared map; concurrent callers can corrupt it.",
    ],
)
def test_real_findings_are_never_container(body):
    """有实质内容的评论不能被误判成容器评论 —— 那等于静默丢掉 finding。"""
    assert cpc.classify_author("coderabbitai[bot]", True, body) != "container"


def test_html_markup_container_shell():
    """coderabbit 的 review_stack_entry 导航壳（dcc-mcp-freecad#34 实测）。

    正文带 HTML 注释与标签，保留标签时长度很大，剥完没有任何实质内容。
    不识别这类会让 S2 总条数随 PR 漂移 —— 正是 PIP-4283 要修的问题。
    """
    body = (
        "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
        "<!-- review_stack_entry_start -->\n\n"
        '<a href="https://app.coderabbit.ai/change-stack/o/r/pull/34?scope=abc">'
        '<img src="https://storage.googleapis.com/x.png" alt=""/></a>'
    )
    assert cpc.classify_author("coderabbitai[bot]", True, body) == "container"


def test_html_wrapped_real_finding_is_not_container():
    """带 HTML 标签但有实质内容 → 不是容器评论。"""
    body = "<p><strong>src/a.py:88</strong> leaks the handle</p>"
    assert cpc.classify_author("coderabbitai[bot]", True, body) != "container"


def test_anchored_comment_is_never_container():
    """锚定到 file:line 的评论一定不是容器评论，即使正文很短。"""
    assert cpc.classify_author("coderabbitai[bot]", True, "nit", "src/a.py", 88) == "ai_reviewer"


def test_inline_file_line_hint_is_not_container():
    """正文自带 file:line 锚点提示 → 不是容器评论。"""
    body = "Bug at src/a.py:88 — the handle is never closed."
    assert cpc.classify_author("coderabbitai[bot]", True, body) != "container"


def test_empty_body_is_not_container():
    assert cpc.classify_author("coderabbitai[bot]", True, "") != "container"


def test_container_counts_toward_total_and_out_of_scope():
    """容器评论计入 total，使 `total = 分类之和` 恒成立。"""
    items = [
        {"author_class": "ai_reviewer"},
        {"author_class": "container"},
        {"author_class": "container"},
        {"author_class": "human"},
        {"author_class": "ci_bot"},
        {"author_class": "self_echo"},
    ]
    counts = cpc.build_counts(items)
    assert counts["total"] == 6
    assert counts["container"] == 2
    identity = cpc.check_identity(counts)
    assert identity["classification_ok"] is True


# ── 验收标准 6：恒等式机械校验 ────────────────────────────────────────────────


def test_classification_identity_holds():
    counts = {
        "total": 10,
        "ai_reviewer": 4,
        "ci_bot": 2,
        "human": 2,
        "self_echo": 1,
        "container": 1,
    }
    assert cpc.check_identity(counts)["classification_ok"] is True


def test_classification_identity_detects_drift():
    """容器评论不计入总条数 → 恒等式破裂，必须能被机械发现。"""
    counts = {
        "total": 10,
        "ai_reviewer": 4,
        "ci_bot": 2,
        "human": 2,
        "self_echo": 1,
        "container": 0,
    }
    result = cpc.check_identity(counts)
    assert result["classification_ok"] is False
    assert result["classification"] == {"left": 10, "right": 9}


def test_verdict_identity_holds():
    """X = ai_reviewer + human = 6 必须等于五态之和。"""
    counts = {
        "total": 10,
        "ai_reviewer": 4,
        "ci_bot": 2,
        "human": 2,
        "self_echo": 1,
        "container": 1,
    }
    verdicts = {"confirmed": 2, "refuted": 1, "stale": 1, "already_addressed": 1, "out_of_scope": 1}
    result = cpc.check_identity(counts, verdicts)
    assert result["verdict_ok"] is True
    assert result["verdict"]["left"] == 6
    assert result["verdict"]["right"] == 6


def test_verdict_identity_detects_miscounted_distribution():
    counts = {
        "total": 10,
        "ai_reviewer": 4,
        "ci_bot": 2,
        "human": 2,
        "self_echo": 1,
        "container": 1,
    }
    verdicts = {"confirmed": 2, "refuted": 1, "stale": 1, "already_addressed": 1, "out_of_scope": 0}
    result = cpc.check_identity(counts, verdicts)
    assert result["verdict_ok"] is False
    assert result["verdict"]["left"] == 6
    assert result["verdict"]["right"] == 5


def test_verdict_identity_skipped_when_no_verdicts():
    counts = {"total": 2, "ai_reviewer": 1, "ci_bot": 0, "human": 1, "self_echo": 0, "container": 0}
    result = cpc.check_identity(counts)
    assert result["verdict_ok"] is None
    assert result["verdict"] is None


def test_all_five_verdict_states_are_known():
    assert cpc.VERDICT_STATES == (
        "confirmed",
        "refuted",
        "stale",
        "already_addressed",
        "out_of_scope",
    )


# ── 验收标准 5：五态裁决台账 ──────────────────────────────────────────────────


def test_verdict_key_shape():
    key = cpc.verdict_key("owner/repo", "coderabbitai[bot]", "src/a.py", 88, "This leaks a handle")
    parts = key.split("|")
    assert len(parts) == 5
    assert parts[0] == "owner/repo"
    assert parts[1] == "coderabbitai[bot]"
    assert parts[2] == "src/a.py"
    assert parts[3] == "80-89"  # 归一化行范围
    assert len(parts[4]) == 16  # sha256 前 16 位


def test_line_range_normalization_buckets_nearby_anchors():
    """bot 微调锚点不应击穿台账：相邻行号归到同一范围。"""
    assert cpc.normalize_line_range(80) == "80-89"
    assert cpc.normalize_line_range(88) == "80-89"
    assert cpc.normalize_line_range(89) == "80-89"
    assert cpc.normalize_line_range(90) == "90-99"
    assert cpc.normalize_line_range(None) == "*"
    assert cpc.normalize_line_range("nope") == "*"


def test_fingerprint_is_truncation_stable():
    """同一条评论的"截断摘录"与"完整正文"必须得到同一指纹。

    采集器只能拿到 excerpt() 截断过的 300 字符，人工补录裁决时用的是完整正文。
    若对全文取哈希，两种来源的指纹不同，台账永远命中不了（dcc-mcp-freecad#34 实测）。
    """
    long_body = "This function leaks the file handle on error. " * 40
    assert len(long_body) > 300
    truncated = cpc.excerpt(long_body, 300)
    assert cpc.text_fingerprint(truncated) == cpc.text_fingerprint(long_body)


def test_fingerprint_differs_for_different_text():
    assert cpc.text_fingerprint("leaks the handle") != cpc.text_fingerprint("off by one bug")


def test_text_fingerprint_is_whitespace_and_case_stable():
    assert cpc.text_fingerprint("This Leaks A Handle!") == cpc.text_fingerprint(
        "this leaks a handle"
    )
    assert cpc.text_fingerprint("") != cpc.text_fingerprint("x")


def test_ledger_round_trip(tmp_path):
    path = tmp_path / "bot_verdict.json"
    ledger = cpc.VerdictLedger(path)
    key = cpc.verdict_key("owner/repo", "coderabbitai[bot]", "src/a.py", 88, "This leaks a handle")

    assert ledger.lookup(key) is None

    ledger.record(key, "refuted", "src/a.py:88 handle released in finally block", "abc123")
    ledger.save()

    reloaded = cpc.VerdictLedger(path)
    hit = reloaded.lookup(key)
    assert hit is not None
    assert hit["verdict"] == "refuted"
    assert hit["head_sha"] == "abc123"
    assert "ts" in hit


def test_ledger_second_hit_reuses_verdict_and_counts_hits(tmp_path):
    """二次命中直接引用历史裁决，不重裁；hits 累加用于热点识别。"""
    path = tmp_path / "l.json"
    ledger = cpc.VerdictLedger(path)
    key = cpc.verdict_key("o/r", "coderabbitai[bot]", "src/a.py", 88, "leak")
    ledger.record(key, "refuted", "already closed in finally", "sha1")
    ledger.save()

    ledger2 = cpc.VerdictLedger(path)
    ledger2.record(key, "refuted", "already closed in finally", "sha2")
    assert ledger2.lookup(key)["hits"] == 2


def test_ledger_rejects_unknown_verdict(tmp_path):
    ledger = cpc.VerdictLedger(tmp_path / "l.json")
    with pytest.raises(ValueError, match="verdict must be one of"):
        ledger.record("some-key", "maybe")


def test_ledger_hotspots_only_after_three_refutes(tmp_path):
    """refuted 累计 <3 不上报，>=3 才汇总 —— 不在每轮重复问 hallong。"""
    path = tmp_path / "l.json"
    ledger = cpc.VerdictLedger(path)
    key = cpc.verdict_key("owner/repo", "coderabbitai[bot]", "src/a.py", 88, "leak")

    ledger.record(key, "refuted", "e1", "sha")
    ledger.record(key, "refuted", "e2", "sha")
    assert ledger.refuted_hotspots() == []

    ledger.record(key, "refuted", "e3", "sha")
    hotspots = ledger.refuted_hotspots()
    assert len(hotspots) == 1
    assert hotspots[0]["bot_login"] == "coderabbitai[bot]"
    assert hotspots[0]["file"] == "src/a.py"
    assert hotspots[0]["count"] == 3


def test_ledger_hotspots_ignore_confirmed(tmp_path):
    ledger = cpc.VerdictLedger(tmp_path / "l.json")
    key = cpc.verdict_key("o/r", "coderabbitai[bot]", "src/b.py", 10, "bug")
    for _ in range(5):
        ledger.record(key, "confirmed", "real bug", "sha")
    assert ledger.refuted_hotspots() == []


def test_ledger_survives_corrupt_file(tmp_path):
    path = tmp_path / "l.json"
    path.write_text("{ not json", encoding="utf-8")
    ledger = cpc.VerdictLedger(path)
    assert ledger.data == {}


def test_ledger_save_without_path_is_noop():
    cpc.VerdictLedger(None).save()  # 不应抛异常


# ── Windows GBK 主机契约 ─────────────────────────────────────────────────────


def test_utf8_force_streams_does_not_crash():
    """collect_pr_context.py 的 UTF-8 强制逻辑不得回退（PIP-3399 试点 2/3）。"""
    src = (SKILL_SCRIPTS / "collect_pr_context.py").read_text(encoding="utf-8")
    assert "_force_utf8_streams" in src
    assert 'reconfigure(encoding="utf-8"' in src


def test_module_has_main_guard():
    src = (SKILL_SCRIPTS / "collect_pr_context.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src


def test_build_counts_is_shared_by_both_paths():
    """GitHub 与工蜂两条采集路径必须共用 build_counts，避免口径漂移。"""
    src = (SKILL_SCRIPTS / "collect_pr_context.py").read_text(encoding="utf-8")
    assert src.count("build_counts(items)") == 2
