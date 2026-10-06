#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR / MR diff 获取工具

支持平台：
  - 工蜂 (git.woa.com)：GitLab MR API
  - GitHub (github.com)：GitHub PR API

用法:
    python fetch_pr_diff.py <pr_or_mr_url> [--output <path>]

输出:
    JSON 文件，包含 base_sha, head_sha, diff_text, files_changed, repo_url, clone_url
    同时 stdout 输出摘要供 Agent 读取

示例:
    python fetch_pr_diff.py https://git.woa.com/lightbox/external/dcc_mcp_maya/-/merge_requests/14
    python fetch_pr_diff.py https://github.com/loonghao/dcc-mcp-core/pull/913
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Union
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ── URL 解析 ──────────────────────────────────────────────────────────────────

def parse_pr_url(url: str) -> dict:
    """
    解析 PR/MR URL，返回平台信息字典。

    工蜂格式:
      https://git.woa.com/{group}/{project}/-/merge_requests/{mr_id}
      https://git.woa.com/{group}/{subgroup}/{project}/-/merge_requests/{mr_id}

    GitHub 格式:
      https://github.com/{owner}/{repo}/pull/{pr_id}
    """
    url = url.rstrip("/")

    # GitHub PR
    m = re.match(
        r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)',
        url
    )
    if m:
        owner, repo, pr_id = m.groups()
        return {
            "platform": "github",
            "owner": owner,
            "repo": repo,
            "pr_id": int(pr_id),
            "clone_url": f"https://github.com/{owner}/{repo}.git",
            "repo_api": f"https://api.github.com/repos/{owner}/{repo}",
            "pr_api": f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_id}",
        }

    # 工蜂 MR（支持多级 namespace）
    m = re.match(
        r'https?://git\.woa\.com/((?:[^/]+/)+[^/]+)/-/merge_requests/(\d+)',
        url
    )
    if m:
        namespace_and_project = m.group(1)  # e.g. "lightbox/external/dcc_mcp_maya"
        mr_id = int(m.group(2))
        # 最后一段是 project，其余是 namespace
        parts = namespace_and_project.split("/")
        project = parts[-1]
        namespace = "/".join(parts[:-1])
        # URL 编码 namespace/project
        encoded_path = namespace_and_project.replace("/", "%2F")
        return {
            "platform": "gongfeng",
            "namespace": namespace,
            "project": project,
            "full_path": namespace_and_project,
            "mr_id": mr_id,
            "clone_url": f"https://git.woa.com/{namespace_and_project}.git",
            "repo_api": f"https://git.woa.com/api/v4/projects/{encoded_path}",
            "mr_api": f"https://git.woa.com/api/v4/projects/{encoded_path}/merge_requests/{mr_id}",
            "diff_api": f"https://git.woa.com/api/v4/projects/{encoded_path}/merge_requests/{mr_id}/diffs",
            "changes_api": f"https://git.woa.com/api/v4/projects/{encoded_path}/merge_requests/{mr_id}/changes",
        }

    return {}


# ── HTTP 工具 ─────────────────────────────────────────────────────────────────

def redact(text: str, secret: Optional[str] = None) -> str:
    """把 token 从任意输出里抹掉。

    git 的错误文本、remote 回显和 .git/config 都可能带出凭据；
    任何打印 stderr 的地方都必须先过这一层。
    """
    if not text:
        return text
    if secret:
        text = text.replace(secret, "***")
    # 兜底：抹掉 URL 里 `//user:pass@host` 形式的凭据
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1***@", text)


def http_get(url: str, headers: Optional[dict] = None, timeout: int = 30) -> Union[dict, str]:
    """发送 GET 请求，返回解析的 JSON 或原始文本"""
    req = Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                try:
                    return json.loads(body)
                except json.JSONDecodeError as e:
                    # Content-Type 声称是 JSON 但体是畸形的 —— 不能让它冒出去
                    print(f"JSON 解析失败 {url}: {e}", file=sys.stderr)
                    return None
            return body
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} from {url}: {body[:300]}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"URL error {url}: {e.reason}", file=sys.stderr)
        return None
    except (TimeoutError, OSError) as e:
        # socket.timeout / TimeoutError 不是 URLError 的子类，必须单独 catch
        print(f"请求超时或 IO 错误 {url}: {e}", file=sys.stderr)
        return None


# ── 工蜂认证 Token ────────────────────────────────────────────────────────────

def get_gongfeng_token() -> Optional[str]:
    """
    按优先级获取工蜂 Private Token:
    1. 环境变量 GF_PRIVATE_TOKEN / GF_TOKEN / GITLAB_TOKEN
    2. git config --global gongfeng.privateToken
    3. ~/.gf_token 文件
    """
    for env in ("GF_PRIVATE_TOKEN", "GF_TOKEN", "GITLAB_TOKEN", "PRIVATE_TOKEN"):
        val = os.environ.get(env, "").strip()
        if val:
            return val

    try:
        r = subprocess.run(
            ["git", "config", "--global", "gongfeng.privateToken"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass

    token_file = Path.home() / ".gf_token"
    if token_file.exists():
        return token_file.read_text().strip()

    return None


def get_github_token() -> Optional[str]:
    """获取 GitHub Token"""
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(env, "").strip()
        if val:
            return val

    try:
        r = subprocess.run(
            ["git", "config", "--global", "github.token"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass

    return None


# ── 工蜂 MR diff 获取 ─────────────────────────────────────────────────────────

def fetch_gongfeng_mr(info: dict, token: Optional[str]) -> Optional[dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token

    # 获取 MR 详情（base/head sha）
    mr_data = http_get(info["mr_api"], headers)
    if not mr_data:
        print("❌ 无法获取 MR 详情，请检查 Token 和网络", file=sys.stderr)
        return None

    base_sha = mr_data.get("diff_refs", {}).get("base_sha", "")
    head_sha = mr_data.get("diff_refs", {}).get("head_sha", "")
    start_sha = mr_data.get("diff_refs", {}).get("start_sha", "")

    title = mr_data.get("title", "")
    description = mr_data.get("description", "") or ""
    source_branch = mr_data.get("source_branch", "")
    target_branch = mr_data.get("target_branch", "")
    state = mr_data.get("state", "")
    author = mr_data.get("author", {}).get("name", "")

    print(f"📋 MR 标题: {title}", file=sys.stderr)
    print(f"   分支: {source_branch} → {target_branch}  状态: {state}  作者: {author}", file=sys.stderr)
    print(f"   base_sha: {base_sha[:8] if base_sha else '?'}  head_sha: {head_sha[:8] if head_sha else '?'}", file=sys.stderr)

    # 获取 changes（含 diff）
    changes_data = http_get(info["changes_api"], headers)
    diff_text = ""
    files_changed = []

    if changes_data and isinstance(changes_data, dict):
        changes = changes_data.get("changes", [])
        for ch in changes:
            old_path = ch.get("old_path", "")
            new_path = ch.get("new_path", "")
            diff_part = ch.get("diff", "")
            is_new = ch.get("new_file", False)
            is_del = ch.get("deleted_file", False)
            is_ren = ch.get("renamed_file", False)

            flag = ""
            if is_new:
                flag = " [NEW]"
            elif is_del:
                flag = " [DELETED]"
            elif is_ren:
                flag = f" [RENAMED from {old_path}]"

            files_changed.append(f"{new_path}{flag}")
            diff_text += f"\ndiff --git a/{old_path} b/{new_path}\n"
            if flag:
                diff_text += f"--- {flag}\n"
            diff_text += diff_part or ""
    else:
        print("⚠️  changes API 返回为空，将尝试 git clone 方式获取 diff", file=sys.stderr)

    return {
        "platform": "gongfeng",
        "title": title,
        "description": description[:500],
        "source_branch": source_branch,
        "target_branch": target_branch,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "author": author,
        "state": state,
        "files_changed": files_changed,
        "diff_text": diff_text,
        "clone_url": info["clone_url"],
        "mr_id": info["mr_id"],
    }


# ── GitHub PR diff 获取 ───────────────────────────────────────────────────────

def fetch_github_pr(info: dict, token: Optional[str]) -> Optional[dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    pr_data = http_get(info["pr_api"], headers)
    if not pr_data:
        print("❌ 无法获取 PR 详情，请检查 Token 和网络", file=sys.stderr)
        return None

    base_sha = pr_data.get("base", {}).get("sha", "")
    head_sha = pr_data.get("head", {}).get("sha", "")
    source_branch = pr_data.get("head", {}).get("ref", "")
    target_branch = pr_data.get("base", {}).get("ref", "")
    title = pr_data.get("title", "")
    description = pr_data.get("body", "") or ""
    state = pr_data.get("state", "")
    author = pr_data.get("user", {}).get("login", "")

    print(f"📋 PR 标题: {title}", file=sys.stderr)
    print(f"   分支: {source_branch} → {target_branch}  状态: {state}  作者: {author}", file=sys.stderr)
    print(f"   base_sha: {base_sha[:8]}  head_sha: {head_sha[:8]}", file=sys.stderr)

    # 获取 diff（使用 diff media type）
    diff_headers = dict(headers)
    diff_headers["Accept"] = "application/vnd.github.diff"
    diff_text = http_get(info["pr_api"], diff_headers)
    if not isinstance(diff_text, str):
        diff_text = ""

    # 获取文件列表
    files_url = info["pr_api"].replace("/pulls/", "/pulls/") + "/files"
    files_data = http_get(files_url, headers)
    files_changed = []
    if files_data and isinstance(files_data, list):
        for f in files_data:
            fname = f.get("filename", "")
            status = f.get("status", "")
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)
            files_changed.append(f"{fname} [{status}] +{additions}/-{deletions}")

    return {
        "platform": "github",
        "title": title,
        "description": description[:500],
        "source_branch": source_branch,
        "target_branch": target_branch,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "author": author,
        "state": state,
        "files_changed": files_changed,
        "diff_text": diff_text,
        "clone_url": info["clone_url"],
        "pr_id": info.get("pr_id"),
    }


# ── git clone + diff fallback ─────────────────────────────────────────────────

def git_clone_and_diff(clone_url: str, base_sha: str, head_sha: str,
                       token: Optional[str], platform: str) -> str:
    """
    克隆仓库，checkout head_sha，然后 git diff base_sha...head_sha。
    适用于 API 无法获取完整 diff 的场景（大 MR 等）。
    """
    if not base_sha or not head_sha:
        print("⚠️  缺少 base/head sha，无法执行 git diff fallback", file=sys.stderr)
        return ""

    # 注入认证：用 `git -c http.extraHeader=...` 而不是把 token 拼进 clone URL。
    # 嵌在 URL 里的 token 会被 git 的错误输出、remote 回显和 .git/config 带出来。
    auth_url = clone_url
    auth_args: List[str] = []
    if token:
        if platform == "gongfeng":
            auth_args = ["-c", f"http.extraHeader=PRIVATE-TOKEN: {token}"]
        elif platform == "github":
            auth_args = ["-c", f"http.extraHeader=AUTHORIZATION: Bearer {token}"]

    with tempfile.TemporaryDirectory(prefix="code_review_pr_") as tmpdir:
        print(f"  克隆到临时目录: {tmpdir}", file=sys.stderr)
        r = subprocess.run(
            ["git", *auth_args, "clone", "--depth=50", auth_url, tmpdir],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            # git 的错误文本常回显远端 URL 与 header —— 打印前先脱敏。
            print(f"  克隆失败: {redact(r.stderr[:300], token)}", file=sys.stderr)
            return ""

        # fetch 两个 sha（shallow clone 可能没有）。必须检查返回码：
        # 早期版本忽略 rc，失败后 diff 静默返回空串，消费者拿到空变更集且退出码 0，
        # 等于"这个 PR 没有改动"。
        for sha in (head_sha, base_sha):
            rf = subprocess.run(
                ["git", *auth_args, "fetch", "--depth=50", "origin", sha],
                capture_output=True, text=True, cwd=tmpdir
            )
            if rf.returncode != 0:
                print(f"  fetch {sha[:12]} 失败: {redact(rf.stderr[:200], token)}",
                      file=sys.stderr)

        # 三点式 `base...head` 需要 merge base，depth=50 的历史常常没有 →
        # 改用两点式 `base head`。API 返回的 base_sha 已经是本 PR 的比较基准，
        # 两点式在语义上就是这次改动。
        r2 = subprocess.run(
            ["git", "diff", base_sha, head_sha],
            capture_output=True, text=True, cwd=tmpdir
        )
        if r2.returncode == 0 and r2.stdout.strip():
            print(f"  git diff 成功，diff 大小: {len(r2.stdout)} bytes", file=sys.stderr)
            return r2.stdout
        if r2.returncode != 0:
            print(f"  git diff 失败: {redact(r2.stderr[:300], token)}", file=sys.stderr)
        else:
            print("  git diff 返回空结果 —— 浅克隆可能缺 merge base，"
                  "视为获取失败而非空 PR", file=sys.stderr)
        return ""


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="获取 PR/MR diff 并输出 JSON")
    parser.add_argument("url", help="PR 或 MR 的完整 URL")
    parser.add_argument("--output", "-o", default=None,
                        help="JSON 输出路径（默认: stdout）")
    parser.add_argument("--no-fallback", action="store_true",
                        help="禁用 git clone fallback")
    args = parser.parse_args()

    info = parse_pr_url(args.url)
    if not info:
        print(f"❌ 无法解析 URL: {args.url}", file=sys.stderr)
        print("支持格式:", file=sys.stderr)
        print("  https://git.woa.com/<namespace>/<project>/-/merge_requests/<id>", file=sys.stderr)
        print("  https://github.com/<owner>/<repo>/pull/<id>", file=sys.stderr)
        sys.exit(1)

    platform = info["platform"]
    print(f"\n🔍 平台: {platform.upper()}", file=sys.stderr)

    if platform == "gongfeng":
        token = get_gongfeng_token()
        if not token:
            print("⚠️  未找到工蜂 Token，尝试匿名访问（可能失败）", file=sys.stderr)
            print("    请设置环境变量 GF_PRIVATE_TOKEN=<your_token>", file=sys.stderr)
        result = fetch_gongfeng_mr(info, token)
        if result and not result.get("diff_text") and not args.no_fallback:
            print("\n🔄 API diff 为空，尝试 git clone fallback...", file=sys.stderr)
            diff = git_clone_and_diff(
                result["clone_url"], result["base_sha"], result["head_sha"],
                token, platform
            )
            result["diff_text"] = diff
            result["diff_source"] = "git_clone"
        elif result:
            result["diff_source"] = "api"

    elif platform == "github":
        token = get_github_token()
        if not token:
            print("⚠️  未找到 GitHub Token，使用匿名访问（可能触发频率限制）", file=sys.stderr)
            print("    请设置环境变量 GITHUB_TOKEN=<your_token>", file=sys.stderr)
        result = fetch_github_pr(info, token)
        if result and not result.get("diff_text") and not args.no_fallback:
            print("\n🔄 API diff 为空，尝试 git clone fallback...", file=sys.stderr)
            diff = git_clone_and_diff(
                result["clone_url"], result["base_sha"], result["head_sha"],
                token, platform
            )
            result["diff_text"] = diff
            result["diff_source"] = "git_clone"
        elif result:
            result["diff_source"] = "api"
    else:
        print(f"❌ 未知平台: {platform}", file=sys.stderr)
        sys.exit(1)

    if not result:
        sys.exit(1)

    # 输出摘要到 stderr（供 Agent 消费）
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  ✅ 获取成功", file=sys.stderr)
    print(f"  标题: {result.get('title', '')}", file=sys.stderr)
    print(f"  作者: {result.get('author', '')}  状态: {result.get('state', '')}", file=sys.stderr)
    print(f"  变更文件数: {len(result.get('files_changed', []))}", file=sys.stderr)
    print(f"  Diff 大小: {len(result.get('diff_text', ''))} chars", file=sys.stderr)
    print(f"  Diff 来源: {result.get('diff_source', 'api')}", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    # 文件列表
    print(f"\n📁 变更文件 ({len(result.get('files_changed', []))} 个):", file=sys.stderr)
    for f in result.get("files_changed", [])[:20]:
        print(f"   {f}", file=sys.stderr)

    # 输出 JSON
    output_data = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output_data, encoding="utf-8")
        print(f"\n💾 JSON 已保存: {args.output}", file=sys.stderr)
    else:
        print(output_data)


if __name__ == "__main__":
    main()
