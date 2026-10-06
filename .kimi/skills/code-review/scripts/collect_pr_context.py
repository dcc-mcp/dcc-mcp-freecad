#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR / MR 评审上下文采集器（Review Context Contract 的 S1–S4）

把自动化 review 需要的外部证据一次抓全，避免 review 只啃 diff：

  S1  CI / check-runs       —— 结论、required 与否、失败 job 日志尾部、flaky 线索
  S2  Bot / AI 评论          —— coderabbitai / copilot / codex / codecov ... 分类与未解决状态
  S3  人工评论与前序轮次      —— unresolved thread、提交时的 head SHA、上轮结论
  S4  关联 issue 上下文       —— closes/fixes 引用的 issue、PIP-XXXX、同文件碰撞的其它 open PR

用法:
    python collect_pr_context.py <pr_or_mr_url> [--output <path>] [--no-logs] [--max-log-lines 40]
    python collect_pr_context.py <url> --verdicts verdicts.json --strict-identity
    python collect_pr_context.py <url> --ledger bot_verdict.json

裁决台账（bot_verdict）与恒等式校验（2026-10-06 PIP-4283）:
    --ledger PATH            读/写裁决台账；命中历史裁决的评论带 prior_verdict，不重裁
    --verdicts JSON|PATH     本次裁决分布 {"confirmed":2,"refuted":1,...}，校验 X = Y+Z+W+V+U
    --record-verdict K=V     直接写一条裁决到台账，如 "owner/repo|bot|f.py|80-89|abc=refuted"
    --strict-identity        恒等式不等时退出码 2（默认只警告）

    台账 key = (repo, bot_login, file, 归一化行范围, 文本指纹)。
    refuted 累计 >=3 次的同类意见会汇总进 ledger_refuted_hotspots，
    由调用方一次性上报 hallong 决策（是否加抑制规则 / 停用该 bot），不在每轮重复问。

    P0 约束：台账只落本地 / Monica，绝不写回 GitHub（不回 bot、不 resolve、不点赞）。

输出:
    JSON 到 stdout（--output 时写入文件），人类可读摘要到 stderr。

平台:
    GitHub  —— 优先用 `gh` CLI（认证与 GraphQL 都靠它），不可用时退回 REST
    工蜂    —— GitLab API v4（PRIVATE-TOKEN）

依赖: Python 3.9+，仅标准库；`gh` 可选但强烈建议（能拿到 review thread 的 resolved 状态和失败日志）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ── 作者分类词表 ──────────────────────────────────────────────────────────────
# 命中即为 AI reviewer：产出的是"Finding"，需要逐条裁决（confirmed/refuted/stale/...）
#
# 维护规则（2026-10-06 PIP-4283）：
#   1. `references/review-context.md` 点名的每个 login 都必须在本表显式命中，
#      不能靠子串误打误撞（如 `chatgpt-codex-connector` 曾只靠 r"codex" 命中）。
#   2. 安全类 bot 属于 ai_reviewer 而非 ci_bot——它们的产出是可裁决的 finding
#      （CodeQL / secret scanning），降格成"信号"就永远不会被逐条裁决。
#   3. 词表顺序即优先级：self_echo > ai_reviewer > ci_bot > 兜底。
AI_REVIEWER_PATTERNS = [
    r"coderabbitai", r"code-rabbit", r"greptile", r"copilot",
    r"chatgpt-codex-connector", r"codex",
    r"cursor", r"sourcery", r"deepsource", r"codium", r"bugbot",
    r"gemini-code-assist", r"anthropic", r"claude", r"reviewbot",
    r"ai-review", r"pr-agent", r"qodo", r"corgea", r"codeant",
    # 安全/依赖扫描类：产出可裁决 finding，不是 CI 信号
    r"github-advanced-security", r"snyk", r"semgrep", r"trivy", r"osv-scanner",
    r"stepsecurity", r"ellipsis", r"graphite", r"codspeed",
]
# 命中即为 CI/质量 bot：产出的是"信号"，落到 S1，不单列 finding
CI_BOT_PATTERNS = [
    r"codecov", r"coveralls", r"sonar", r"dependabot", r"renovate",
    r"github-actions", r"gitlab-ci", r"jenkins", r"buildbot", r"pre-commit",
    r"allcontributors", r"imgbot", r"stale",
]
# 我们自己 agent 镜像过去的评论：跳过，不构成输入（避免自证循环）
#
# 必须在 ai_reviewer 之前判定：AI_REVIEWER_PATTERNS 含 r"claude"/r"anthropic"，
# 我们自己的 claude 系 agent 若对外发过 GitHub 评论，会被误判成外部 AI reviewer
# 拿去裁决——那正是自证循环。这里要覆盖我们全部对外评论身份。
SELF_ECHO_PATTERNS = [
    r"monica", r"pipelineDev", r"loonghao-agent",
    r"loonghao", r"hallong", r"xiaoli", r"qianqian",
    r"a7020420-eb91", r"4ef9454a-16f4",
    r"^dcc-mcp-agent", r"review-ai-bot",
]

MONICA_ISSUE_RE = re.compile(r"\b(PIP|[A-Z]{2,8})-\d{1,6}\b")
LINKED_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*(?:([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+))?#(\d+)",
    re.IGNORECASE,
)

TERMINAL_FAILURE = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
WAITING = {"pending", "queued", "in_progress", "waiting", "requested", "expected", "neutral"}

# ── 容器评论（container comments）识别 ─────────────────────────────────────────
# 同一批 bot 评论里混着两类不含独立 finding 的评论：
#   - 汇总型：只报计数（"Actionable comments posted: 3" / "5 issues found"）
#   - 入口型：只给导航（walkthrough / "View reviewed changes" / change-stack）
# 识别点：没有锚定到具体 file:line 的 finding，正文只有计数或导航。
# 这类评论计入 out_of_scope 且计入 S2 总条数 —— 否则 `总条数 = 五态之和` 不成立，
# 不同 PR 的裁决分布行无法横向比较（2026-09-22 试点 1 vs 试点 2 的根因）。
SUMMARY_CONTAINER_RE = re.compile(
    r"^\s*(?:actionable\s+comments?\s+posted\s*:\s*\d+|"
    r"\d+\s+(?:issues?|comments?|findings?|suggestions?)\s+found|"
    r"(?:this\s+pr|this\s+merge\s+request)\s+was\s+reviewed|"
    r"(?:found|reviewed)\s+\d+\s+(?:issues?|comments?|findings?)|"
    r"(?:no|0)\s+(?:new\s+)?(?:issues?|comments?|findings?|suggestions?|problems?|blocking\s+issues?)"
    r"(?:\s+(?:found|detected|to\s+report))?|"
    r"all\s+checks?\s+passed|lgtm)\s*[.!]?\s*$",
    re.IGNORECASE,
)
NAV_CONTAINER_RE = re.compile(
    r"(?:view\s+reviewed\s+changes|view\s+changes|see\s+the\s+walkthrough|"
    r"walkthrough|change[- ]?stack|commit\s+range|view\s+full\s+report|"
    r"full\s+report|see\s+all\s+comments|view\s+all\s+comments)",
    re.IGNORECASE,
)
# 正文里出现这些，说明它锚定了具体代码 —— 不是容器评论
ANCHOR_HINT_RE = re.compile(r"(?:[\w./\\-]+\.(?:py|rs|ts|tsx|js|jsx|go|cpp|c|h|md|toml|ya?ml|json|sh)\s*:\s*\d+)")
# 剥掉 HTML 注释 / 标签后正文基本为空 = 纯机器生成的容器壳
HTML_NOISE_RE = re.compile(r"(?:<!--.*?-->|<[^>]+>)", re.DOTALL)
# 自动生成的导航壳：coderabbit 的 review_stack_entry / change-stack 之类
AUTOGEN_MARKERS = (
    "auto-generated comment", "review_stack_entry", "change-stack",
    "app.coderabbit.ai", "coderabbit.ai",
)

# 五态裁决。容器评论固定落 out_of_scope，其余由 review 逐条裁。
VERDICT_STATES = ("confirmed", "refuted", "stale", "already_addressed", "out_of_scope")


def _force_utf8_streams() -> None:
    """Windows/zh-CN 主机的 locale 是 cp936(GBK)：

    - 读入侧：`text=True` 会按 GBK 解码 `gh` 的 UTF-8 输出 -> UnicodeDecodeError 直接崩；
    - 输出侧：摘要里的框线字符与 CJK 在 GBK 控制台写不出 -> UnicodeEncodeError。

    两头都显式固定成 UTF-8 + replace，避免每次都要靠 `PYTHONUTF8=1` 绕过。
    （PIP-3399 试点 2/3 实测：不修则采集器在 GBK 主机上必崩。）
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 非 TTY / 被包装的流没有 reconfigure
            pass


_force_utf8_streams()


# ── 通用工具 ──────────────────────────────────────────────────────────────────

def run(cmd: List[str], timeout: int = 60) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {' '.join(cmd[:3])}"
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def gh_json(args: List[str], timeout: int = 60) -> Tuple[Optional[Any], Optional[str]]:
    """调用 `gh api ...` 并解析 JSON。返回 (data, error)。"""
    code, out, err = run(["gh", "api", *args], timeout=timeout)
    if code != 0:
        return None, (err.strip() or out.strip() or f"gh exit {code}")
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, f"invalid json from gh: {exc}"


def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Tuple[Optional[Any], Optional[str]]:
    req = Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return json.loads(body), None
            return body, None
    except HTTPError as exc:
        return None, f"HTTP {exc.code} {url}"
    except URLError as exc:
        return None, f"URL error {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{url}: {exc}"


def build_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """S2 计数与恒等式自检（2026-10-06 PIP-4283）。

    GitHub 与工蜂两条采集路径共用，避免两边口径漂移。
    `container` 计入 total 且固定贡献 out_of_scope，使
    `total = ai_reviewer + ci_bot + human + self_echo + container` 恒成立。
    """
    return {
        "total": len(items),
        "ai_reviewer": sum(1 for i in items if i["author_class"] == "ai_reviewer"),
        "ci_bot": sum(1 for i in items if i["author_class"] == "ci_bot"),
        "human": sum(1 for i in items if i["author_class"] == "human"),
        "self_echo": sum(1 for i in items if i["author_class"] == "self_echo"),
        "container": sum(1 for i in items if i["author_class"] == "container"),
        "unresolved_ai_reviewer": sum(1 for i in items
                                      if i["author_class"] == "ai_reviewer"
                                      and i.get("resolved") is False),
        "unresolved_human": sum(1 for i in items
                                if i["author_class"] == "human"
                                and i.get("resolved") is False),
        "stale_vs_head": sum(1 for i in items if i.get("stale_vs_head")),
    }


def check_identity(counts: Dict[str, int], verdicts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """校验两条恒等式，不等即拒绝输出 Review context 块。

    1. 分类恒等式：total = ai_reviewer + ci_bot + human + self_echo + container
    2. 裁决恒等式：X = confirmed + refuted + stale + already_addressed + out_of_scope
                  其中 X = 需要逐条裁决的条数（ai_reviewer + human）
    """
    total = counts.get("total", 0)
    classified = sum(counts.get(k, 0) for k in
                     ("ai_reviewer", "ci_bot", "human", "self_echo", "container"))
    out: Dict[str, Any] = {
        "classification_ok": total == classified,
        "classification": {"left": total, "right": classified},
        "verdict_ok": None,
        "verdict": None,
    }
    if verdicts is not None:
        expected = counts.get("ai_reviewer", 0) + counts.get("human", 0)
        right = sum(verdicts.get(k, 0) for k in VERDICT_STATES)
        out["verdict_ok"] = expected == right
        out["verdict"] = {"left": expected, "right": right, "states": dict(verdicts)}
    return out


def is_container_comment(body: str, path: Optional[str] = None,
                         line: Optional[Any] = None) -> bool:
    """汇总型/入口型评论 = 没有锚定到 file:line 的独立 finding。

    容器评论计入 out_of_scope 且计入 S2 总条数，使 `总条数 = 五态之和` 恒成立。
    判定顺序：有锚点 → 不是容器；正文只有计数或导航 → 容器。
    """
    if path and line is not None:
        return False
    text = (body or "").strip()
    if not text:
        return False
    if ANCHOR_HINT_RE.search(text):
        return False
    if SUMMARY_CONTAINER_RE.match(text):
        return True

    # 剥掉 HTML 注释与标签再看：coderabbit 的 review_stack_entry 之类整条都是
    # 机器生成的导航壳，保留标签时长度很大，剥完几乎没有实质内容（PR #34 实测）。
    plain = HTML_NOISE_RE.sub(" ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain and any(mk in text for mk in AUTOGEN_MARKERS):
        return True

    # 导航型：正文短（<= 400 字符）且通篇是导航链接/入口，没有别的实质内容
    check = plain or text
    if NAV_CONTAINER_RE.search(check) and len(check) <= 400:
        stripped = NAV_CONTAINER_RE.sub("", check)
        stripped = re.sub(r"[\s\W_]+", "", stripped)
        if len(stripped) <= 40:
            return True
    return False


def classify_author(login: str, is_bot: bool = False, body: str = "",
                    path: Optional[str] = None, line: Optional[Any] = None) -> str:
    """human | ai_reviewer | ci_bot | self_echo | container

    `container` 是独立于作者身份的类别：容器评论的作者通常仍是 ai_reviewer，
    但它本身不含可裁决的 finding，因此单列一态，固定落 out_of_scope。
    """
    if is_container_comment(body, path, line):
        return "container"
    low = (login or "").lower()
    if not low:
        return "ci_bot" if is_bot else "human"
    for pat in SELF_ECHO_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return "self_echo"
    for pat in AI_REVIEWER_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return "ai_reviewer"
    for pat in CI_BOT_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return "ci_bot"
    return "ci_bot" if is_bot else "human"


def excerpt(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " …[truncated]"


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# CI 日志里真正有用的行：Actions 的 ##[error]、pytest 的 FAILED、Rust 的 error[E...]、断言与 traceback
ERROR_MARKERS = ("##[error]", "FAILED", "Error:", "error[E", "panicked at",
                 "assert", "AssertionError", "Traceback", "Process completed with exit code")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


# GitHub Actions 每个 job 的头尾都会灌入 checkout / cleanup 样板行，
# 纯尾部截取会被它们占满，把真正的报错挤出去（2026-09-22 实测 dcc-mcp-photoshop#113）。
NOISE_PATTERNS = (
    "##[group]", "##[endgroup]", "[command]", "Cleaning up orphan processes",
    "Post job cleanup", "Adding repository directory", "Temporarily overriding HOME",
    "Removing SSH command configuration", "Removing HTTP extra header",
    "Removing includeIf entries", "Copying '", "git version ",
    "is being deprecated", "is deprecated", "##[warning]Node.js",
    "__ has been deprecated", "Set up job", "Complete job",
)


def tail_lines(text: str, n: int) -> str:
    """取日志尾部，但优先保留真正的报错行——纯尾部常被 env dump 占满。

    两个必须同时成立的约束（缺一个报错行就会被挤掉）：
    1. 命中 ERROR_MARKERS 的行优先占位，且不得被后续尾部行挤出窗口；
    2. 用于补位的尾部行先滤掉 Actions 的 setup / cleanup 样板。
    """
    lines = [ln.rstrip() for ln in strip_ansi(text).splitlines() if ln.strip()]
    if not lines:
        return ""

    seen: set = set()
    hits: List[str] = []
    for ln in lines:
        if ln in seen:
            continue
        if any(mk in ln for mk in ERROR_MARKERS):
            seen.add(ln)
            hits.append(ln)

    quota = max(n // 2, 5)
    picked = hits[-quota:]
    if len(picked) >= n:
        return "\n".join(picked)

    rest = n - len(picked)
    picked_set = set(picked)
    tail = [ln for ln in lines if ln not in picked_set]
    filler = [ln for ln in tail if not any(np in ln for np in NOISE_PATTERNS)]
    if len(filler) < rest:
        filler = tail  # 信号不足时退回原始尾部，宁可吵也别漏
    filler = filler[-rest:]

    order = {ln: i for i, ln in enumerate(lines)}
    merged = sorted(picked + [ln for ln in filler if ln not in picked_set],
                    key=lambda ln: order.get(ln, 0))
    return "\n".join(merged[-n:])


# ── URL 解析 ──────────────────────────────────────────────────────────────────

def parse_pr_url(url: str) -> Dict[str, Any]:
    url = url.rstrip("/")
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if m:
        owner, repo, pr_id = m.groups()
        return {
            "platform": "github",
            "owner": owner,
            "repo": repo,
            "slug": f"{owner}/{repo}",
            "number": int(pr_id),
            "pr_url": f"https://github.com/{owner}/{repo}/pull/{pr_id}",
        }
    m = re.match(r"https?://git\.woa\.com/((?:[^/]+/)+[^/]+)/-/merge_requests/(\d+)", url)
    if m:
        full_path, mr_id = m.groups()
        encoded = full_path.replace("/", "%2F")
        return {
            "platform": "gongfeng",
            "full_path": full_path,
            "encoded_path": encoded,
            "number": int(mr_id),
            "pr_url": url,
            "base_api": f"https://git.woa.com/api/v4/projects/{encoded}",
        }
    return {}


# ── 认证 ──────────────────────────────────────────────────────────────────────

def get_token(kind: str) -> Optional[str]:
    if kind == "github":
        envs = ("GITHUB_TOKEN", "GH_TOKEN")
    else:
        envs = ("GF_PRIVATE_TOKEN", "GF_TOKEN", "GITLAB_TOKEN", "PRIVATE_TOKEN")
    for env in envs:
        val = os.environ.get(env, "").strip()
        if val:
            return val
    cfg_key = "github.token" if kind == "github" else "gongfeng.privateToken"
    code, out, _ = run(["git", "config", "--global", cfg_key], timeout=5)
    if code == 0 and out.strip():
        return out.strip()
    if kind != "github":
        token_file = Path.home() / ".gf_token"
        if token_file.exists():
            return token_file.read_text(encoding="utf-8").strip()
    return None


# ── S1: CI ────────────────────────────────────────────────────────────────────

def collect_ci_github(info: Dict[str, Any], head_sha: str, base_branch: str,
                      want_logs: bool, max_log_lines: int, gaps: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "checks": [], "required_contexts": [], "summary": {},
        "verdict": "unknown", "failure_logs": [],
    }

    checks: List[Dict[str, Any]] = []
    err = None
    data, err = gh_json([f"repos/{info['slug']}/commits/{head_sha}/check-runs",
                         "--paginate",
                         "-X", "GET",
                         "-f", "per_page=100"], timeout=90)
    if data and isinstance(data, dict):
        # NB: do not name the loop variable `run` — it shadows the module-level
        # run() helper used further down for `gh run view --log-failed`.
        for cr in data.get("check_runs", []):
            conclusion = (cr.get("conclusion") or "").lower()
            state = (cr.get("status") or "").lower()
            url = cr.get("details_url") or ""
            run_id = None
            job_id = None
            m = re.search(r"/actions/runs/(\d+)", url)
            if m:
                run_id = m.group(1)
            mj = re.search(r"/actions/runs/\d+/job/(\d+)", url)
            if mj:
                job_id = mj.group(1)
            checks.append({
                "name": cr.get("name"),
                "state": state,
                "conclusion": conclusion or None,
                "required": None,  # filled below when branch protection is readable
                "url": url,
                "run_id": run_id,
                "job_id": job_id,
                "started_at": cr.get("started_at"),
                "completed_at": cr.get("completed_at"),
            })
    if err:
        gaps.append(f"check-runs 读取失败（{err}）")

    # required 上下文来自分支保护；读不到就保持 None 并在 gaps 里说明
    req_data, req_err = gh_json([f"repos/{info['slug']}/branches/{base_branch}/protection/required_status_checks/contexts"])
    required_set = set()
    if isinstance(req_data, list):
        required_set = {str(x) for x in req_data}
        out["required_contexts"] = sorted(required_set)
    elif req_err:
        gaps.append(f"required checks 配置不可读（{req_err}）——本次按实际检查集合判定，required 标记为 null")

    for chk in checks:
        if required_set:
            chk["required"] = chk["name"] in required_set

    out["checks"] = checks
    total = len(checks)
    failing = [c for c in checks if (c.get("conclusion") or "") in TERMINAL_FAILURE]
    pending = [c for c in checks if (c.get("conclusion") or "") not in TERMINAL_FAILURE
               and (c.get("state") or "") in WAITING]
    success = [c for c in checks if (c.get("conclusion") or "") == "success"]
    out["summary"] = {
        "total": total,
        "required_total": len(required_set) if required_set else None,
        "success": len(success),
        "failing": len(failing),
        "pending": len(pending),
    }

    if not checks:
        out["verdict"] = "unknown"
        gaps.append("未读到任何 check-run")
    elif failing:
        out["verdict"] = "failing"
    elif pending:
        out["verdict"] = "pending"
    else:
        out["verdict"] = "green"

    if want_logs and failing:
        for chk in failing[:3]:
            rid = chk.get("run_id")
            jid = chk.get("job_id")
            if not rid and not jid:
                continue
            # 优先按 job 取日志：同一 run 内多个 job 会共用 run_id，
            # 用 `gh run view <run> --log-failed` 会把别的 job 的行混进本 check 的摘要。
            if jid:
                code, log, log_err = run(["gh", "run", "view", "--job", jid,
                                          "--repo", info["slug"], "--log"], timeout=180)
            else:
                code, log, log_err = 1, "", "no job_id"
            if (code != 0 or not log.strip()) and rid:
                code, log, log_err = run(["gh", "run", "view", rid,
                                          "--repo", info["slug"], "--log-failed"], timeout=180)
            if code == 0 and log.strip():
                out["failure_logs"].append({
                    "check": chk.get("name"),
                    "run_id": rid,
                    "job_id": jid,
                    "excerpt": tail_lines(log, max_log_lines),
                })
            else:
                gaps.append(f"失败日志不可读（check={chk.get('name')} run={rid} job={jid}: "
                            f"{(log_err or log or '').strip()[:160]}）")
    return out


def collect_ci_gongfeng(info: Dict[str, Any], iid: int, token: Optional[str],
                        want_logs: bool, max_log_lines: int, gaps: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "checks": [], "required_contexts": [], "summary": {},
        "verdict": "unknown", "failure_logs": [],
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token

    pipes, err = http_get(f"{info['base_api']}/merge_requests/{iid}/pipelines", headers)
    if err or not isinstance(pipes, list):
        gaps.append(f"工蜂 pipeline 不可读（{err or 'empty'}）")
        return out

    pipe = pipes[0] if pipes else None
    if not pipe:
        gaps.append("工蜂 MR 无关联 pipeline")
        return out

    jobs, err = http_get(f"{info['base_api']}/pipelines/{pipe.get('id')}/jobs?per_page=100", headers)
    if err or not isinstance(jobs, list):
        gaps.append(f"工蜂 jobs 不可读（{err or 'empty'}）")
        return out

    for job in jobs:
        status = (job.get("status") or "").lower()
        conclusion_map = {"success": "success", "failed": "failure", "canceled": "cancelled",
                          "skipped": "skipped", "manual": "neutral"}
        conclusion = conclusion_map.get(status)
        out["checks"].append({
            "name": job.get("name"),
            "state": "completed" if conclusion else status,
            "conclusion": conclusion,
            "required": bool(job.get("allow_failure") is False),
            "url": job.get("web_url"),
            "run_id": job.get("id"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("finished_at"),
        })

    failing = [c for c in out["checks"] if (c.get("conclusion") or "") in TERMINAL_FAILURE]
    pending = [c for c in out["checks"] if not c.get("conclusion")]
    success = [c for c in out["checks"] if c.get("conclusion") == "success"]
    out["summary"] = {
        "total": len(out["checks"]),
        "required_total": sum(1 for c in out["checks"] if c.get("required")),
        "success": len(success),
        "failing": len(failing),
        "pending": len(pending),
    }
    out["verdict"] = "failing" if failing else ("pending" if pending else "green")

    if want_logs and failing and token:
        for chk in failing[:3]:
            trace, err = http_get(f"{info['base_api']}/jobs/{chk.get('run_id')}/trace", headers)
            if isinstance(trace, str) and trace.strip():
                out["failure_logs"].append({
                    "check": chk.get("name"), "job_id": chk.get("run_id"),
                    "excerpt": tail_lines(trace, max_log_lines),
                })
            else:
                gaps.append(f"工蜂 job trace 不可读（job={chk.get('run_id')}）")
    return out


# ── S2/S3: 评论与前序轮次 ─────────────────────────────────────────────────────

REVIEW_THREADS_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$cursor){
        pageInfo{hasNextPage endCursor}
        nodes{
          isResolved isOutdated
          comments(first:20){
            nodes{
              author{login __typename}
              body path line originalLine originalCommit{oid}
              createdAt url
            }
          }
        }
      }
    }
  }
}
"""


def collect_comments_github(info: Dict[str, Any], number: int,
                            head_sha: str, gaps: List[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"items": [], "counts": {}, "review_rounds": []}
    items: List[Dict[str, Any]] = []

    # 1) review threads（可解析的 inline 讨论，含 resolved 状态）
    cursor = None
    threads_seen = False
    for _ in range(5):
        variables = {"owner": info["owner"], "repo": info["repo"], "number": number}
        if cursor:
            variables["cursor"] = cursor
        args = ["graphql", "-f", f"query={REVIEW_THREADS_QUERY}",
                "-F", f"owner={info['owner']}", "-F", f"repo={info['repo']}",
                "-F", f"number={number}"]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        data, err = gh_json(args, timeout=90)
        if not data:
            gaps.append(f"review threads 不可读（{err}）")
            break
        threads_seen = True
        pr = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        threads = (pr.get("reviewThreads") or {}).get("nodes") or []
        for th in threads:
            resolved = bool(th.get("isResolved"))
            outdated = bool(th.get("isOutdated"))
            for c in (th.get("comments") or {}).get("nodes") or []:
                author = (c.get("author") or {}).get("login") or "unknown"
                is_bot = (c.get("author") or {}).get("__typename") == "Bot"
                commit_id = ((c.get("originalCommit") or {}).get("oid") or "")[:40]
                items.append({
                    "source": "review_thread",
                    "author": author,
                    "author_class": classify_author(
                        author, is_bot, c.get("body") or "",
                        c.get("path"), c.get("line") or c.get("originalLine")),
                    "resolved": resolved,
                    "outdated": outdated,
                    "stale_vs_head": bool(commit_id and head_sha and commit_id != head_sha),
                    "commit_id": commit_id or None,
                    "path": c.get("path"),
                    "line": c.get("line") or c.get("originalLine"),
                    "created_at": c.get("createdAt"),
                    "url": c.get("url"),
                    "body_excerpt": excerpt(c.get("body"), 300),
                })
        page = (pr.get("reviewThreads") or {}).get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    if not threads_seen:
        gaps.append("review threads 未取到（可能缺 gh 或权限）——unresolved blocking 判定降级")

    # 2) issue-level 评论（PR 会话区）
    data, err = gh_json([f"repos/{info['slug']}/issues/{number}/comments",
                         "-X", "GET", "--paginate", "-f", "per_page=100"])
    if isinstance(data, list):
        for c in data:
            author = ((c.get("user") or {}).get("login")) or "unknown"
            is_bot = ((c.get("user") or {}).get("type")) == "Bot"
            items.append({
                "source": "issue_comment",
                "author": author,
                "author_class": classify_author(author, is_bot, c.get("body") or ""),
                "resolved": None,
                "outdated": None,
                "stale_vs_head": None,
                "commit_id": None,
                "path": None,
                "line": None,
                "created_at": c.get("created_at"),
                "url": c.get("html_url"),
                "body_excerpt": excerpt(c.get("body"), 300),
            })
    elif err:
        gaps.append(f"issue comments 不可读（{err}）")

    # 3) review 提交（前序轮次）
    data, err = gh_json([f"repos/{info['slug']}/pulls/{number}/reviews",
                         "-X", "GET", "--paginate", "-f", "per_page=100"])
    if isinstance(data, list):
        for rv in data:
            author = ((rv.get("user") or {}).get("login")) or "unknown"
            result["review_rounds"].append({
                "author": author,
                "author_class": classify_author(author, ((rv.get("user") or {}).get("type")) == "Bot"),
                "state": rv.get("state"),
                "submitted_at": rv.get("submitted_at"),
                "commit_id": (rv.get("commit_id") or "")[:40] or None,
                "stale_vs_head": bool(rv.get("commit_id") and head_sha and rv["commit_id"][:40] != head_sha),
            })
            body = (rv.get("body") or "").strip()
            if body and rv.get("state") != "APPROVED":
                items.append({
                    "source": "review_body",
                    "author": author,
                    "author_class": classify_author(
                        author, ((rv.get("user") or {}).get("type")) == "Bot", body),
                    "resolved": None,
                    "outdated": None,
                    "stale_vs_head": bool(rv.get("commit_id") and head_sha and rv["commit_id"][:40] != head_sha),
                    "commit_id": (rv.get("commit_id") or "")[:40] or None,
                    "path": None,
                    "line": None,
                    "created_at": rv.get("submitted_at"),
                    "url": rv.get("html_url"),
                    "body_excerpt": excerpt(body, 300),
                })
    elif err:
        gaps.append(f"reviews 不可读（{err}）")

    # 4) inline review comments（GraphQL 不可用时的兜底，保证 inline 评论不丢）
    if not any(i["source"] == "review_thread" for i in items):
        data, err = gh_json([f"repos/{info['slug']}/pulls/{number}/comments",
                         "-X", "GET", "--paginate", "-f", "per_page=100"])
        if isinstance(data, list):
            for c in data:
                author = ((c.get("user") or {}).get("login")) or "unknown"
                commit_id = (c.get("original_commit_id") or "")[:40]
                items.append({
                    "source": "review_comment",
                    "author": author,
                    "author_class": classify_author(
                        author, ((c.get("user") or {}).get("type")) == "Bot",
                        c.get("body") or "", c.get("path"),
                        c.get("line") or c.get("original_line")),
                    "resolved": None,
                    "outdated": None,
                    "stale_vs_head": bool(commit_id and head_sha and commit_id != head_sha),
                    "commit_id": commit_id or None,
                    "path": c.get("path"),
                    "line": c.get("line") or c.get("original_line"),
                    "created_at": c.get("created_at"),
                    "url": c.get("html_url"),
                    "body_excerpt": excerpt(c.get("body"), 300),
                })

    # 排序：未解决优先，AI reviewer 优先，时间倒序
    priority = {"ai_reviewer": 0, "human": 1, "ci_bot": 2, "self_echo": 3, "container": 4}
    items.sort(key=lambda x: (
        0 if x.get("resolved") is False else 1,
        priority.get(x.get("author_class"), 5),
        x.get("created_at") or "",
    ))
    items.reverse()

    result["items"] = items
    result["counts"] = build_counts(items)
    result["identity"] = check_identity(result["counts"])
    return result


def collect_comments_gongfeng(info: Dict[str, Any], iid: int, token: Optional[str],
                              head_sha: str, gaps: List[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"items": [], "counts": {}, "review_rounds": []}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token

    items: List[Dict[str, Any]] = []
    discussions, err = http_get(f"{info['base_api']}/merge_requests/{iid}/discussions?per_page=100", headers)
    if isinstance(discussions, list):
        for disc in discussions:
            resolved = bool(disc.get("resolved"))
            for note in disc.get("notes") or []:
                if note.get("system"):
                    continue
                author = ((note.get("author") or {}).get("username")) or "unknown"
                pos = note.get("position") or {}
                commit_id = (pos.get("head_sha") or "")[:40]
                items.append({
                    "source": "discussion",
                    "author": author,
                    "author_class": classify_author(
                        author, False, note.get("body") or "",
                        pos.get("new_path") or pos.get("old_path"),
                        pos.get("new_line") or pos.get("old_line")),
                    "resolved": resolved if disc.get("resolvable") else None,
                    "outdated": bool(pos.get("outdated")),
                    "stale_vs_head": bool(commit_id and head_sha and commit_id != head_sha),
                    "commit_id": commit_id or None,
                    "path": pos.get("new_path") or pos.get("old_path"),
                    "line": pos.get("new_line") or pos.get("old_line"),
                    "created_at": note.get("created_at"),
                    "url": note.get("web_url"),
                    "body_excerpt": excerpt(note.get("body"), 300),
                })
    elif err:
        gaps.append(f"工蜂 discussions 不可读（{err}）")

    # 非讨论区的普通备注
    notes, err = http_get(f"{info['base_api']}/merge_requests/{iid}/notes?per_page=100&sort=asc", headers)
    if isinstance(notes, list):
        for note in notes:
            if note.get("system") or note.get("resolvable"):
                continue
            author = ((note.get("author") or {}).get("username")) or "unknown"
            items.append({
                "source": "note",
                "author": author,
                "author_class": classify_author(author, False, note.get("body") or ""),
                "resolved": None,
                "outdated": None,
                "stale_vs_head": None,
                "commit_id": None,
                "path": None,
                "line": None,
                "created_at": note.get("created_at"),
                "url": note.get("web_url"),
                "body_excerpt": excerpt(note.get("body"), 300),
            })
    elif err:
        gaps.append(f"工蜂 notes 不可读（{err}）")

    result["items"] = items
    result["counts"] = build_counts(items)
    result["identity"] = check_identity(result["counts"])
    return result


# ── bot_verdict 裁决台账 ──────────────────────────────────────────────────────
# 五态裁决只活在当次 review 正文时，下一轮 / 下一个 PR 撞到同一条 bot 意见要重裁一遍。
# 台账按 (repo, bot_login, file, 归一化行范围, 文本指纹) 落盘，二次命中直接引用历史裁决。
#
# P0 约束：台账全部落在本地 / Monica，绝不写回 GitHub（不回 bot、不 resolve、不点赞）。

def normalize_line_range(line: Optional[Any]) -> str:
    """行号归一化：相近的行号视为同一范围，避免 bot 微调锚点就击穿台账。"""
    if line is None:
        return "*"
    try:
        n = int(line)
    except (TypeError, ValueError):
        return "*"
    bucket = (n // 10) * 10
    return f"{bucket}-{bucket + 9}"


FINGERPRINT_CHARS = 200


def text_fingerprint(body: str) -> str:
    """文本指纹：归一化空白与标点后取前 N 字符，再取 sha256 前 16 位。

    必须**先截断再哈希**：采集器里能拿到的正文常常是 `excerpt()` 截断过的 300 字符，
    而人工补录裁决时用的是完整正文。若对全文取哈希，同一条评论在两种来源下会得到
    不同指纹，台账永远命中不了（dcc-mcp-freecad#34 实测踩到）。
    """
    import hashlib
    norm = re.sub(r"\s+", " ", (body or "").strip().lower())
    norm = re.sub(r"[^\w ]", "", norm)
    return hashlib.sha256(norm[:FINGERPRINT_CHARS].encode("utf-8")).hexdigest()[:16]


def verdict_key(repo: str, bot_login: str, path: Optional[str],
                line: Optional[Any], body: str) -> str:
    """台账 key = (repo, bot_login, file, 归一化行范围, 文本指纹)。"""
    return "|".join([
        repo or "*",
        (bot_login or "unknown").lower(),
        path or "*",
        normalize_line_range(line),
        text_fingerprint(body),
    ])


class VerdictLedger:
    """裁决台账：读写 JSON 文件，二次命中引用历史裁决而不重裁。

    文件结构：{ "<verdict_key>": { "verdict": ..., "evidence": ...,
                                    "head_sha": ..., "ts": ..., "hits": N } }
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.data: Dict[str, Any] = {}
        if path and Path(path).exists():
            try:
                self.data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        return self.data.get(key)

    def record(self, key: str, verdict: str, evidence: str = "",
               head_sha: str = "") -> None:
        if verdict not in VERDICT_STATES:
            raise ValueError(f"verdict must be one of {VERDICT_STATES}, got {verdict!r}")
        import datetime
        entry = self.data.get(key)
        if entry:
            entry["hits"] = int(entry.get("hits", 1)) + 1
            entry["verdict"] = verdict
            entry["evidence"] = evidence
            entry["head_sha"] = head_sha
            entry["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        else:
            self.data[key] = {
                "verdict": verdict,
                "evidence": evidence,
                "head_sha": head_sha,
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "hits": 1,
            }

    def refuted_hotspots(self, threshold: int = 3) -> List[Dict[str, Any]]:
        """同一 bot_login+file 的 refuted 累计 >= threshold → 汇总走一次 hallong 决策。

        不在每轮重复问：只在这里给出"够阈值了"的清单，由调用方决定是否上报。
        """
        tally: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for key, entry in self.data.items():
            if entry.get("verdict") != "refuted":
                continue
            parts = key.split("|")
            if len(parts) < 5:
                continue
            repo, bot, path = parts[0], parts[1], parts[2]
            agg = tally.setdefault((bot, path), {"repo": repo, "bot_login": bot,
                                                 "file": path, "count": 0, "samples": []})
            agg["count"] += int(entry.get("hits", 1))
            if len(agg["samples"]) < 3:
                agg["samples"].append(entry.get("evidence", "")[:120])
        return sorted((v for v in tally.values() if v["count"] >= threshold),
                      key=lambda v: -v["count"])

    def save(self) -> None:
        if not self.path:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path).write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── S4: 关联 issue 与同文件碰撞 ───────────────────────────────────────────────

def collect_linked_github(info: Dict[str, Any], number: int, body: str,
                          changed_files: List[str], gaps: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "keyword_refs": [], "monica_refs": [], "linked_issues": [], "related_open_prs": [],
    }
    body = body or ""

    for m in LINKED_ISSUE_RE.finditer(body):
        out["keyword_refs"].append({"raw": m.group(0), "repo": m.group(1), "number": int(m.group(2))})
    out["monica_refs"] = sorted(set(MONICA_ISSUE_RE.findall(body)))

    for ref in out["keyword_refs"][:5]:
        slug = ref["repo"] or info["slug"]
        data, err = gh_json([f"repos/{slug}/issues/{ref['number']}"])
        if isinstance(data, dict):
            out["linked_issues"].append({
                "repo": slug,
                "number": ref["number"],
                "title": data.get("title"),
                "state": data.get("state"),
                "is_pull_request": bool(data.get("pull_request")),
                "url": data.get("html_url"),
                "body_excerpt": excerpt(data.get("body"), 600),
            })
        elif err:
            gaps.append(f"关联 issue {slug}#{ref['number']} 不可读（{err}）")

    # 同文件碰撞的其它 open PR
    if changed_files:
        data, err = gh_json([f"repos/{info['slug']}/pulls",
                             "-X", "GET",
                             "-f", "state=open",
                             "-f", "per_page=100"], timeout=90)
        if isinstance(data, list):
            changed = {f.split(" ")[0] for f in changed_files}
            for pr in data:
                if pr.get("number") == number:
                    continue
                files = pr.get("files") or []
                paths = {f.get("filename") if isinstance(f, dict) else str(f) for f in files}
                if not paths:
                    continue
                overlap = sorted(changed & paths)
                if overlap:
                    out["related_open_prs"].append({
                        "number": pr.get("number"),
                        "title": pr.get("title"),
                        "head_sha": (pr.get("head") or {}).get("sha"),
                        "url": pr.get("html_url"),
                        "overlap_files": overlap[:10],
                    })
        elif err:
            gaps.append(f"同仓库 open PR 列表不可读（{err}）——未做文件碰撞检查")
    return out


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="采集 PR/MR 评审上下文（CI / bot 评论 / 关联 issue）")
    ap.add_argument("url", help="PR 或 MR 的完整 URL")
    ap.add_argument("--output", "-o", default=None, help="JSON 输出路径（默认 stdout）")
    ap.add_argument("--no-logs", action="store_true", help="跳过失败日志抓取")
    ap.add_argument("--max-log-lines", type=int, default=40, help="每条失败日志保留的尾部行数")
    ap.add_argument("--changed-files", default=None,
                    help="变更文件列表文件（每行一个）。不传时尝试从 PR API 推断")
    ap.add_argument("--ledger", default=None,
                    help="bot_verdict 裁决台账路径（JSON）。命中历史裁决时直接引用，不重裁")
    ap.add_argument("--verdicts", default=None,
                    help="本次裁决分布 JSON，形如 {\"confirmed\":2,\"refuted\":1,...}。"
                         "传入后校验裁决恒等式 X = Y+Z+W+V+U，不等则拒绝输出 Review context 块")
    ap.add_argument("--record-verdict", action="append", default=[], metavar="KEY=VERDICT",
                    help="写入一条裁决到台账，如 "
                         "\"owner/repo|coderabbitai|src/a.py|80-89|abc123=refuted\"（可重复）")
    ap.add_argument("--strict-identity", action="store_true",
                    help="恒等式不等时以退出码 2 失败（默认只警告）")
    args = ap.parse_args()

    ledger = VerdictLedger(Path(args.ledger)) if args.ledger else None
    if ledger and args.record_verdict:
        for pair in args.record_verdict:
            if "=" not in pair:
                print(f"忽略非法 --record-verdict（缺 =）：{pair}", file=sys.stderr)
                continue
            key, verdict = pair.rsplit("=", 1)
            try:
                ledger.record(key, verdict.strip(), head_sha="")
            except ValueError as exc:
                print(f"忽略非法 --record-verdict：{exc}", file=sys.stderr)
        ledger.save()
        return 0

    info = parse_pr_url(args.url)
    if not info:
        print("无法解析 URL，支持：", file=sys.stderr)
        print("  https://github.com/<owner>/<repo>/pull/<id>", file=sys.stderr)
        print("  https://git.woa.com/<ns>/<proj>/-/merge_requests/<id>", file=sys.stderr)
        return 1

    gaps: List[str] = []
    platform = info["platform"]

    if platform == "github" and not shutil.which("gh"):
        gaps.append("未找到 `gh` CLI —— GitHub 上下文降级为 REST 只读，review thread resolved 状态与失败日志不可得")

    # ── 基础元数据 ──
    meta: Dict[str, Any] = {}
    changed_files: List[str] = []
    if platform == "github":
        data, err = gh_json([f"repos/{info['slug']}/pulls/{info['number']}"])
        if isinstance(data, dict):
            meta = {
                "title": data.get("title"),
                "body": data.get("body") or "",
                "author": ((data.get("user") or {}).get("login")),
                "state": data.get("state"),
                "draft": bool(data.get("draft")),
                "head_sha": (data.get("head") or {}).get("sha"),
                "base_branch": (data.get("base") or {}).get("ref"),
                "head_branch": (data.get("head") or {}).get("ref"),
                "mergeable": data.get("mergeable"),
                "mergeable_state": data.get("mergeable_state"),
                "review_decision": None,
            }
        elif err:
            gaps.append(f"PR 元数据不可读（{err}）")
        # reviewDecision 走 GraphQL 或 search；这里用 REST 的 reviews 汇总代替，标注为派生值
        if meta.get("head_sha"):
            files_data, ferr = gh_json([f"repos/{info['slug']}/pulls/{info['number']}/files",
                                        "-X", "GET", "--paginate", "-f", "per_page=100"])
            if isinstance(files_data, list):
                changed_files = [f.get("filename", "") for f in files_data]
            elif ferr:
                gaps.append(f"PR files 不可读（{ferr}）")
    else:
        token = get_token("gongfeng")
        if not token:
            gaps.append("未找到工蜂 Token（GF_PRIVATE_TOKEN / git config gongfeng.privateToken）——只读接口可能受限")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["PRIVATE-TOKEN"] = token
        data, err = http_get(f"{info['base_api']}/merge_requests/{info['number']}", headers)
        if isinstance(data, dict):
            meta = {
                "title": data.get("title"),
                "body": data.get("description") or "",
                "author": ((data.get("author") or {}).get("username")),
                "state": data.get("state"),
                "draft": bool(data.get("work_in_progress")),
                "head_sha": (data.get("diff_refs") or {}).get("head_sha") or data.get("sha"),
                "base_branch": data.get("target_branch"),
                "head_branch": data.get("source_branch"),
                "mergeable": None,
                "review_decision": None,
            }
        elif err:
            gaps.append(f"MR 元数据不可读（{err}）")
        changes, cerr = http_get(f"{info['base_api']}/merge_requests/{info['number']}/changes", headers)
        if isinstance(changes, dict):
            changed_files = [c.get("new_path", "") for c in (changes.get("changes") or [])]
        elif cerr:
            gaps.append(f"MR changes 不可读（{cerr}）")

    if args.changed_files:
        p = Path(args.changed_files)
        if p.exists():
            changed_files = [ln.strip() for ln in
                             p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    head_sha = meta.get("head_sha") or ""
    if not head_sha:
        gaps.append("未取到 head SHA —— stale 判定不可用")

    # ── S1 ──
    if platform == "github":
        ci = collect_ci_github(info, head_sha, meta.get("base_branch") or "main",
                               not args.no_logs, args.max_log_lines, gaps)
    else:
        ci = collect_ci_gongfeng(info, info["number"], get_token("gongfeng"),
                                 not args.no_logs, args.max_log_lines, gaps)

    # ── S2/S3 ──
    if platform == "github":
        comments = collect_comments_github(info, info["number"], head_sha, gaps)
        linked = collect_linked_github(info, info["number"], meta.get("body") or "",
                                       changed_files, gaps)
    else:
        comments = collect_comments_gongfeng(info, info["number"], get_token("gongfeng"),
                                             head_sha, gaps)
        linked = {"keyword_refs": [], "monica_refs": sorted(set(
            MONICA_ISSUE_RE.findall(meta.get("body") or ""))),
            "linked_issues": [], "related_open_prs": [],
            "note": "工蜂 MR 的关联 issue / 同文件碰撞检查未实现，需人工确认"}

    # ── 裁决台账：二次命中的 bot 意见直接引用历史裁决，不重裁 ──
    repo_slug = info.get("slug") or info.get("full_path") or "*"
    reused: List[Dict[str, Any]] = []
    if ledger:
        for item in comments.get("items", []):
            if item.get("author_class") not in ("ai_reviewer", "human"):
                continue
            key = verdict_key(repo_slug, item.get("author"), item.get("path"),
                              item.get("line"), item.get("body_excerpt") or "")
            item["verdict_key"] = key
            hit = ledger.lookup(key)
            if hit:
                item["prior_verdict"] = hit
                reused.append({"key": key, "author": item.get("author"),
                               "verdict": hit.get("verdict"),
                               "file": item.get("path"), "line": item.get("line"),
                               "hits": hit.get("hits")})
        comments["ledger_reused"] = reused
        comments["ledger_refuted_hotspots"] = ledger.refuted_hotspots()

    # ── 恒等式机械校验 ──
    verdicts: Optional[Dict[str, int]] = None
    if args.verdicts:
        try:
            verdicts = json.loads(Path(args.verdicts).read_text(encoding="utf-8")
                                  if Path(args.verdicts).exists() else args.verdicts)
        except (json.JSONDecodeError, OSError) as exc:
            gaps.append(f"--verdicts 解析失败（{exc}）——裁决恒等式未校验")
    identity = check_identity(comments.get("counts", {}), verdicts)
    if not identity["classification_ok"]:
        gaps.append(f"分类恒等式不成立：total={identity['classification']['left']} "
                    f"≠ 分类之和={identity['classification']['right']}")
    if identity["verdict_ok"] is False:
        gaps.append(f"裁决恒等式不成立：需裁决 {identity['verdict']['left']} 条 "
                    f"≠ 五态之和 {identity['verdict']['right']}")
    comments["identity"] = identity

    payload: Dict[str, Any] = {
        "platform": platform,
        "pr_url": info["pr_url"],
        "number": info["number"],
        "repo": info.get("slug") or info.get("full_path"),
        "meta": meta,
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "ci": ci,
        "comments": comments,
        "linked": linked,
        "gaps": gaps,
    }

    # ── 摘要（stderr）──
    s = ci.get("summary", {})
    c = comments.get("counts", {})
    print("\n" + "=" * 64, file=sys.stderr)
    print(f"  Review context — {info['pr_url']}", file=sys.stderr)
    print(f"  head: {(head_sha or '?')[:12]}  base: {meta.get('base_branch')}  "
          f"files: {len(changed_files)}", file=sys.stderr)
    print(f"  CI[{ci.get('verdict')}]: total={s.get('total')} success={s.get('success')} "
          f"failing={s.get('failing')} pending={s.get('pending')} "
          f"required={s.get('required_total')}", file=sys.stderr)
    for fl in ci.get("failure_logs", []):
        print(f"    ✗ {fl.get('check')} — 日志 {len(fl.get('excerpt', '').splitlines())} 行", file=sys.stderr)
    print(f"  评论: total={c.get('total')} ai_reviewer={c.get('ai_reviewer')} "
          f"human={c.get('human')} ci_bot={c.get('ci_bot')} self_echo={c.get('self_echo')} "
          f"container={c.get('container')}", file=sys.stderr)
    print(f"    unresolved: ai={c.get('unresolved_ai_reviewer')} human={c.get('unresolved_human')} "
          f"| stale_vs_head={c.get('stale_vs_head')}", file=sys.stderr)
    if c.get("container"):
        print(f"    已识别并排除 {c.get('container')} 条容器评论（汇总型/入口型，计入 out_of_scope）",
              file=sys.stderr)
    ident = comments.get("identity") or {}
    if ident:
        print(f"    恒等式: 分类 {'OK' if ident.get('classification_ok') else 'FAIL'} "
              f"({ident['classification']['left']}={ident['classification']['right']})"
              + (f" | 裁决 {'OK' if ident.get('verdict_ok') else 'FAIL'} "
                 f"({ident['verdict']['left']}={ident['verdict']['right']})"
                 if ident.get("verdict") else " | 裁决 未校验（未传 --verdicts）"),
              file=sys.stderr)
    if reused:
        print(f"    台账复用 {len(reused)} 条历史裁决（不重裁）", file=sys.stderr)
    hotspots = comments.get("ledger_refuted_hotspots") or []
    if hotspots:
        print(f"    ⚠ refuted 热点（>=3 次，建议一次性上报 hallong 决策）:", file=sys.stderr)
        for h in hotspots:
            print(f"      - {h['bot_login']} @ {h['file']} ×{h['count']}", file=sys.stderr)
    print(f"  关联 issue: {len(linked.get('linked_issues', []))} 条 | "
          f"Monica refs: {', '.join(linked.get('monica_refs') or []) or '无'} | "
          f"同文件 open PR: {len(linked.get('related_open_prs', []))}", file=sys.stderr)
    if gaps:
        print(f"  ⚠ 上下文缺口 {len(gaps)} 项:", file=sys.stderr)
        for g in gaps:
            print(f"    - {g}", file=sys.stderr)
    print("=" * 64 + "\n", file=sys.stderr)

    out = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"JSON 已保存: {args.output}", file=sys.stderr)
    else:
        print(out)

    if args.strict_identity and identity and (
            not identity["classification_ok"] or identity["verdict_ok"] is False):
        print("恒等式校验失败（--strict-identity）：拒绝输出 Review context 块",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
