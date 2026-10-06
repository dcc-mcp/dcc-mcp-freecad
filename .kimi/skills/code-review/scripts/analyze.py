#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码点评静态分析脚本 — 多语言版

支持语言: Python / Rust / Node.js / TypeScript / Go / 微服务混合

用法:
    python analyze.py <repo_dir> [--lang python|rust|node|go|auto] [--focus <subdir1> ...]

例如:
    python analyze.py /path/to/repo
    python analyze.py /path/to/repo --lang rust
    python analyze.py /path/to/repo --focus src services
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


# ── 配置 ──────────────────────────────────────────────────────────────────────

LONG_FUNC_THRESHOLD = 50
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "target", "node_modules", ".venv", "venv",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
}

DANGEROUS_CALLS_PYTHON = ["exit(", "os.system(", "os.popen(", "subprocess.call(", "eval(", "exec("]
DANGEROUS_CALLS_RUST   = ["unwrap()", "expect(", "panic!(", ".unsafe", "unsafe {", "unsafe{"]
DANGEROUS_CALLS_NODE   = ["eval(", "exec(", "execSync(", "Function("]

KNOWN_TYPOS = [
    "SCERET", "SECERT", "genreate", "recieve", "occured", "seperator",
    "definitly", "adress", "calender", "refrence", "smaple", "beging",
]

# ── 语言检测 ──────────────────────────────────────────────────────────────────

def detect_language(root: Path) -> dict:
    """自动识别项目语言和类型"""
    cargo_files  = list(iter_files(root, "Cargo.toml"))
    pkg_files    = list(iter_files(root, "package.json"))
    go_files     = list(iter_files(root, "go.mod"))
    py_files     = list(iter_files(root, "*.py"))
    ts_files     = list(iter_files(root, "*.ts"))
    proto_files  = list(iter_files(root, "*.proto"))

    has_docker_compose = (root / "docker-compose.yml").exists() or (root / "docker-compose.yaml").exists()
    has_k8s = (root / "k8s").exists() or (root / "helm").exists()

    # 微服务判断：多个独立服务目录，各自带自己的构建文件
    service_dirs = []
    for sub in root.iterdir():
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        has_own_build = (
            (sub / "Cargo.toml").exists() or
            (sub / "package.json").exists() or
            (sub / "pyproject.toml").exists() or
            (sub / "go.mod").exists()
        )
        if has_own_build:
            service_dirs.append(sub)

    is_microservice = len(service_dirs) >= 2 or has_docker_compose or has_k8s

    # 主语言判断
    counts = {
        "rust":   len(cargo_files),
        "node":   len(pkg_files),
        "go":     len(go_files),
        "python": len(py_files),
    }
    if is_microservice and len([v for v in counts.values() if v > 0]) >= 2:
        primary_lang = "multi"
    elif counts["rust"] and counts["rust"] >= max(counts.values()):
        primary_lang = "rust"
    elif counts["node"] and len(ts_files) > 0:
        primary_lang = "typescript"
    elif counts["node"]:
        primary_lang = "node"
    elif counts["go"]:
        primary_lang = "go"
    else:
        primary_lang = "python"

    return {
        "lang": primary_lang,
        "is_microservice": is_microservice,
        "has_proto": len(proto_files) > 0,
        "proto_files": [str(p.relative_to(root)) for p in proto_files],
        "services": [str(s.relative_to(root)) for s in service_dirs],
        "ts_files": len(ts_files),
    }


# ── 通用工具 ──────────────────────────────────────────────────────────────────

def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def is_excluded(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in EXCLUDED_DIRS for part in rel_parts)


def iter_files(root: Path, pattern: str):
    for path in root.rglob(pattern):
        if not is_excluded(path, root):
            yield path


def expand_focus(root: Path, focus: list) -> list:
    expanded = []
    for item in focus:
        item = item.lstrip("/")
        if "*" in item:
            prefix = item.rstrip("*")
            matched = [d.name for d in root.iterdir() if d.is_dir() and d.name.startswith(prefix)]
            expanded.extend(matched) if matched else print(f"  [WARN] 无匹配: {item}", file=sys.stderr)
        else:
            expanded.append(item)
    return expanded


def collect_files(root: Path, focus: list, extensions: list) -> list:
    """按扩展名收集文件，支持 focus 过滤"""
    if focus:
        resolved = expand_focus(root, focus)
        files = []
        for subdir in resolved:
            target = root / subdir
            if target.exists():
                for ext in extensions:
                    files.extend(iter_files(target, f"*{ext}"))
        return files
    result = []
    for ext in extensions:
        result.extend(iter_files(root, f"*{ext}"))
    return result


def check_long_functions(files: list, root: Path, patterns: list) -> list:
    """通用超长函数检测，patterns 是函数定义的正则列表"""
    hits = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        func_start, func_name = -1, ""
        for i, line in enumerate(lines):
            matched = False
            for pat in patterns:
                m = re.match(pat, line)
                if m:
                    if func_start >= 0 and (i - func_start) > LONG_FUNC_THRESHOLD:
                        hits.append({"file": rel(f, root), "func": func_name,
                                     "lines": i - func_start, "start": func_start + 1})
                    func_start, func_name = i, m.group(1)
                    matched = True
                    break
    return sorted(hits, key=lambda x: -x["lines"])


def check_commented_code(files: list, patterns: list) -> int:
    total = 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            for pat in patterns:
                if re.match(pat, line):
                    total += 1
                    break
    return total


def check_typos(files: list, root: Path) -> list:
    hits = []
    seen = set()
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            for typo in KNOWN_TYPOS:
                key = (typo, rel(f, root))
                if typo in line and key not in seen:
                    hits.append({"file": rel(f, root), "line": i, "typo": typo, "content": line.strip()[:80]})
                    seen.add(key)
    return hits


def check_hardcoded_config(files: list, root: Path, patterns: list) -> list:
    """检测硬编码配置（端口、地址、密钥等）"""
    hits = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith(("//", "#", "*", "/*")):
                continue
            for pat, label in patterns:
                if re.search(pat, line):
                    hits.append({"file": rel(f, root), "line": i, "label": label, "content": stripped[:80]})
                    break
    return hits[:20]  # 最多返回 20 条


def check_duplicate_filenames(root: Path, extensions: list) -> list:
    name_map = defaultdict(list)
    for ext in extensions:
        for f in iter_files(root, f"*{ext}"):
            name_map[f.name].append(rel(f, root))
    return [(name, paths) for name, paths in name_map.items() if len(paths) > 1]


# ── Python 专项 ───────────────────────────────────────────────────────────────

def analyze_python(root: Path, focus: list) -> dict:
    files = collect_files(root, focus, [".py"])
    # 过滤 test 文件看是否有差异，但仍分析所有
    files = [f for f in files if ".git" not in str(f) and "__pycache__" not in str(f)]

    dangerous = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("#"):
                continue
            for pat in DANGEROUS_CALLS_PYTHON:
                if pat in line:
                    dangerous.append({"file": rel(f, root), "line": i, "pattern": pat, "content": line.strip()[:80]})

    getenv_map = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        cnt = len(re.findall(r'os\.getenv', text))
        if cnt:
            getenv_map[rel(f, root)] = cnt

    long_funcs = check_long_functions(files, root, [r'^[ ]{0,8}(?:async\s+)?def\s+(\w+)\('])

    commented = check_commented_code(files, [
        r'^\s*#\s+(import |from |def |class |async |await |if |for |return |print\(|logger\.)'
    ])

    # 类型注解 & docstring
    has_ann, no_ann, has_doc, no_doc = 0, 0, 0, 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if re.match(r'^\s{0,8}(?:async\s+)?def\s+\w+\(', line):
                if "->" in line or (i+1 < len(lines) and "->" in lines[i+1]):
                    has_ann += 1
                else:
                    no_ann += 1
                if re.match(r'^\s{0,8}(?:async\s+)?def\s+[^_]', line):
                    nxt = lines[i+1].strip() if i+1 < len(lines) else ""
                    if nxt.startswith('"""') or nxt.startswith("'''"):
                        has_doc += 1
                    else:
                        no_doc += 1

    # sys.path hack
    path_hacks = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if "sys.path.append" in line and not line.strip().startswith("#"):
                path_hacks.append({"file": rel(f, root), "line": i})

    hardcoded = check_hardcoded_config(files, root, [
        (r'(?i)(password|secret|token|api_key)\s*=\s*["\'][^"\']{6,}', "硬编码密钥"),
        (r':\d{4,5}["\']\s*$|localhost:\d{4,5}', "硬编码端口/地址"),
    ])

    total = has_ann + no_ann
    total_pub = has_doc + no_doc
    return {
        "files": len(files),
        "dangerous": dangerous,
        "getenv": getenv_map,
        "long_funcs": long_funcs,
        "commented": commented,
        "typos": check_typos(files, root),
        "path_hacks": path_hacks,
        "hardcoded": hardcoded,
        "dups": check_duplicate_filenames(root, [".py"]),
        "type_ann_rate": round(has_ann/total*100, 1) if total else 0,
        "has_ann": has_ann, "no_ann": no_ann,
        "docstring_rate": round(has_doc/total_pub*100, 1) if total_pub else 0,
        "has_doc": has_doc, "no_doc": no_doc,
    }


# ── Rust 专项 ─────────────────────────────────────────────────────────────────

def analyze_rust(root: Path, focus: list) -> dict:
    files = collect_files(root, focus, [".rs"])
    files = [f for f in files if ".git" not in str(f)]

    dangerous = []
    unsafe_blocks = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            for pat in DANGEROUS_CALLS_RUST:
                if pat in line:
                    dangerous.append({"file": rel(f, root), "line": i, "pattern": pat, "content": stripped[:80]})
            if re.search(r'\bunsafe\s*\{', line):
                # 检查前一行是否有注释说明
                prev = lines[i-2].strip() if i >= 2 else ""
                has_comment = prev.startswith("//")
                unsafe_blocks.append({"file": rel(f, root), "line": i, "has_comment": has_comment, "content": stripped[:80]})

    # unwrap/expect 专项（生产路径，排除 test）
    unwrap_hits = []
    for h in dangerous:
        if "unwrap()" in h["pattern"] or "expect(" in h["pattern"]:
            unwrap_hits.append(h)
    # 排除 test 文件
    unwrap_prod = [h for h in unwrap_hits if "_test" not in h["file"] and "tests/" not in h["file"]]

    long_funcs = check_long_functions(files, root, [
        r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[\(<]',
        r'^\s*(?:pub\s+)?(?:async\s+)?(?:extern "C"\s+)?fn\s+(\w+)',
    ])

    commented = check_commented_code(files, [
        r'^\s*//\s*(use |mod |fn |let |pub |impl |struct |enum |async )'
    ])

    # doc comment 覆盖率（pub fn 是否有 ///）
    has_doc, no_doc = 0, 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if re.match(r'^\s*pub\s+(?:async\s+)?fn\s+', line):
                prev = lines[i-1].strip() if i > 0 else ""
                if prev.startswith("///") or prev.startswith("#["):
                    has_doc += 1
                else:
                    no_doc += 1

    hardcoded = check_hardcoded_config(files, root, [
        (r'(?i)(password|secret|token|api_key)\s*=\s*"[^"]{6,}"', "硬编码密钥"),
        (r'"(?:localhost|127\.0\.0\.1):\d{4,5}"', "硬编码地址"),
        (r':\s*\d{4,5}\s*(?://|$)', "硬编码端口"),
    ])

    total_pub = has_doc + no_doc
    return {
        "files": len(files),
        "dangerous": dangerous,
        "unwrap_prod": unwrap_prod,
        "unsafe_blocks": unsafe_blocks,
        "unsafe_without_comment": [u for u in unsafe_blocks if not u["has_comment"]],
        "long_funcs": long_funcs,
        "commented": commented,
        "typos": check_typos(files, root),
        "hardcoded": hardcoded,
        "dups": check_duplicate_filenames(root, [".rs"]),
        "docstring_rate": round(has_doc/total_pub*100, 1) if total_pub else 0,
        "has_doc": has_doc, "no_doc": no_doc,
    }


# ── Node.js / TypeScript 专项 ─────────────────────────────────────────────────

def analyze_node(root: Path, focus: list) -> dict:
    ts_files = collect_files(root, focus, [".ts"])
    js_files = collect_files(root, focus, [".js"])
    ts_files = [f for f in ts_files if ".git" not in str(f) and "node_modules" not in str(f) and ".d.ts" not in str(f)]
    js_files = [f for f in js_files if ".git" not in str(f) and "node_modules" not in str(f)]
    files = ts_files + js_files
    is_ts = len(ts_files) > 0

    dangerous = []
    any_hits = []
    ts_ignore_hits = []
    unhandled_promise = []
    process_env_map = {}

    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            text = "\n".join(lines)
        except Exception:
            continue
        frel = rel(f, root)

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            for pat in DANGEROUS_CALLS_NODE:
                if pat in line:
                    dangerous.append({"file": frel, "line": i, "pattern": pat, "content": stripped[:80]})

            # TypeScript any
            if is_ts and ": any" in line and not stripped.startswith("//"):
                any_hits.append({"file": frel, "line": i, "content": stripped[:80]})

            # @ts-ignore / @ts-nocheck
            if "@ts-ignore" in line or "@ts-nocheck" in line:
                ts_ignore_hits.append({"file": frel, "line": i, "content": stripped[:80]})

            # process.env 散布
            if "process.env." in line and not stripped.startswith("//"):
                process_env_map[frel] = process_env_map.get(frel, 0) + 1

        # unhandled promise: .then( 无 .catch(
        then_no_catch = len(re.findall(r'\.then\(', text)) - len(re.findall(r'\.catch\(', text))
        if then_no_catch > 0:
            unhandled_promise.append({"file": frel, "unhandled": then_no_catch})

    long_funcs = check_long_functions(files, root, [
        r'^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(<]',
        r'^\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(',
        r'^\s*(?:public|private|protected|static|async)?\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::\s*\w+)?\s*\{',
    ])

    commented = check_commented_code(files, [
        r'^\s*//\s*(import |const |let |var |function |class |if |for |return |console\.)'
    ])

    # JSDoc 覆盖率（export function 是否有 /** */）
    has_doc, no_doc = 0, 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if re.match(r'^\s*(?:export\s+)?(?:async\s+)?function\s+\w+', line):
                prev = lines[i-1].strip() if i > 0 else ""
                if prev.endswith("*/") or prev.startswith("/**") or prev.startswith("*"):
                    has_doc += 1
                else:
                    no_doc += 1

    hardcoded = check_hardcoded_config(files, root, [
        (r'(?i)(password|secret|token|apiKey|api_key)\s*[:=]\s*["\'][^"\']{6,}', "硬编码密钥"),
        (r'["\'](?:localhost|127\.0\.0\.1):\d{4,5}["\']', "硬编码地址"),
        (r'PORT\s*=\s*\d{4,5}', "硬编码端口"),
    ])

    total_pub = has_doc + no_doc
    return {
        "files": len(files),
        "is_ts": is_ts,
        "dangerous": dangerous,
        "any_hits": any_hits,
        "ts_ignore": ts_ignore_hits,
        "unhandled_promise": unhandled_promise,
        "process_env": process_env_map,
        "long_funcs": long_funcs,
        "commented": commented,
        "typos": check_typos(files, root),
        "hardcoded": hardcoded,
        "dups": check_duplicate_filenames(root, [".ts", ".js"]),
        "jsdoc_rate": round(has_doc/total_pub*100, 1) if total_pub else 0,
        "has_doc": has_doc, "no_doc": no_doc,
    }


# ── 微服务专项 ────────────────────────────────────────────────────────────────

def analyze_microservice(root: Path, info: dict) -> dict:
    results = {}

    # proto 文件分布
    proto_files = list(iter_files(root, "*.proto"))
    proto_dirs = set(str(p.parent.relative_to(root)) for p in proto_files)

    # 硬编码服务地址
    all_files = []
    for ext in [".py", ".rs", ".ts", ".js", ".go"]:
        all_files.extend(iter_files(root, f"*{ext}"))

    hardcoded_svc = []
    for f in all_files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            # 硬编码 service URL
            if re.search(r'http://[a-z-]+(?:-service|-svc)(?::\d+)?', line) or \
               re.search(r'grpc://[a-z-]+(?:-service|-svc)(?::\d+)?', line):
                hardcoded_svc.append({"file": rel(f, root), "line": i, "content": line.strip()[:80]})

    # 各服务有无统一日志格式 (简单检测有无 traceId/requestId 字段)
    services_with_trace = set()
    for f in all_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if re.search(r'traceId|trace_id|requestId|request_id|X-Request-ID', text, re.IGNORECASE):
            svc = str(Path(rel(f, root)).parts[0]) if "/" in rel(f, root) else "root"
            services_with_trace.add(svc)

    results["services"] = info.get("services", [])
    results["proto_files"] = info.get("proto_files", [])
    results["proto_dirs"] = sorted(proto_dirs)
    results["hardcoded_svc"] = hardcoded_svc[:10]
    results["services_with_trace"] = sorted(services_with_trace)
    results["total_services"] = len(info.get("services", []))

    return results


# ── 输出格式化 ────────────────────────────────────────────────────────────────

def print_section(title: str, count: int, items: list, formatter, max_items: int = 10):
    icon = "🔴" if "危险" in title or "unsafe" in title.lower() else "🟡"
    print(f"\n{icon} {title} ({count} 处):")
    if not items:
        print("   无")
        return
    for item in items[:max_items]:
        print(f"   {formatter(item)}")
    if len(items) > max_items:
        print(f"   ... 共 {len(items)} 处")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_dir")
    parser.add_argument("--lang", default="auto",
                        choices=["auto", "python", "rust", "node", "typescript", "go", "multi"])
    parser.add_argument("--focus", nargs="*", default=[])
    args = parser.parse_args()

    root = Path(args.repo_dir)
    if not root.exists():
        print(f"错误：目录不存在: {root}", file=sys.stderr)
        sys.exit(1)

    # 语言检测
    if args.lang == "auto":
        info = detect_language(root)
        lang = info["lang"]
    else:
        lang = args.lang
        info = detect_language(root)  # 仍需检测微服务信息

    print(f"\n{'='*60}")
    print(f"  代码静态分析: {root.name}")
    print(f"  语言: {lang.upper()}  |  微服务: {'是' if info['is_microservice'] else '否'}")
    if info.get("services"):
        print(f"  检测到服务: {', '.join(info['services'][:6])}")
    if args.focus:
        print(f"  重点目录: {', '.join(args.focus)}")
    print(f"{'='*60}\n")

    # ── 按语言分析 ──────────────────────────────────────────────────────────────

    if lang == "rust":
        r = analyze_rust(root, args.focus)
        print(f"📁 扫描 .rs 文件数: {r['files']}")

        print_section("危险调用（含 unwrap/panic）", len(r['dangerous']), r['dangerous'],
                      lambda h: f"[{h['pattern']}] {h['file']}:{h['line']}  {h['content']}")

        print(f"\n🔴 生产路径 unwrap()/expect() ({len(r['unwrap_prod'])} 处):")
        for h in r['unwrap_prod'][:10]:
            print(f"   {h['file']}:{h['line']}  {h['content']}")
        if not r['unwrap_prod']:
            print("   无")

        print(f"\n🔴 unsafe 块 ({len(r['unsafe_blocks'])} 个，其中 {len(r['unsafe_without_comment'])} 个无注释说明):")
        for u in r['unsafe_without_comment'][:5]:
            print(f"   {u['file']}:{u['line']}  {u['content']}")
        if not r['unsafe_without_comment']:
            print("   无未注释 unsafe")

        print_section("超长函数 >50行", len(r['long_funcs']), r['long_funcs'],
                      lambda f: f"{f['lines']}L  {f['file']}::{f['func']} (L{f['start']})")
        print(f"\n🟡 注释掉的代码行: {r['commented']}")
        print_section("已知拼写错误", len(r['typos']), r['typos'],
                      lambda h: f"[{h['typo']}] {h['file']}:{h['line']}")
        print_section("硬编码配置", len(r['hardcoded']), r['hardcoded'],
                      lambda h: f"[{h['label']}] {h['file']}:{h['line']}  {h['content']}")
        print(f"\n📝 pub fn doc-comment 覆盖率: {r['docstring_rate']}%  (有 {r['has_doc']}，无 {r['no_doc']})")

        print(f"\n{'='*60}\n  汇总\n{'='*60}")
        print(f"  危险调用(含unwrap):  {len(r['dangerous'])} 处")
        print(f"  生产 unwrap/expect:  {len(r['unwrap_prod'])} 处")
        print(f"  unsafe 块(无注释):   {len(r['unsafe_without_comment'])} 个")
        print(f"  超长函数(>50L):      {len(r['long_funcs'])} 个")
        print(f"  注释代码行:          {r['commented']} 行")
        print(f"  拼写错误:            {len(r['typos'])} 处")
        print(f"  硬编码配置:          {len(r['hardcoded'])} 处")
        print(f"  doc-comment覆盖率:   {r['docstring_rate']}%")

    elif lang in ("node", "typescript"):
        r = analyze_node(root, args.focus)
        label = "TypeScript" if r['is_ts'] else "JavaScript"
        print(f"📁 扫描 .ts/.js 文件数: {r['files']}")

        print_section("危险调用(eval/exec)", len(r['dangerous']), r['dangerous'],
                      lambda h: f"[{h['pattern']}] {h['file']}:{h['line']}  {h['content']}")

        if r['is_ts']:
            print(f"\n🔴 TypeScript any 类型滥用 ({len(r['any_hits'])} 处):")
            for h in r['any_hits'][:10]:
                print(f"   {h['file']}:{h['line']}  {h['content']}")
            if not r['any_hits']:
                print("   无")

            print(f"\n🔴 @ts-ignore / @ts-nocheck ({len(r['ts_ignore'])} 处):")
            for h in r['ts_ignore'][:5]:
                print(f"   {h['file']}:{h['line']}  {h['content']}")
            if not r['ts_ignore']:
                print("   无")

        print(f"\n🟡 未处理的 Promise rejection ({len(r['unhandled_promise'])} 个文件):")
        for u in r['unhandled_promise'][:5]:
            print(f"   {u['file']}  ({u['unhandled']} 个 .then 无对应 .catch)")
        if not r['unhandled_promise']:
            print("   无")

        total_env = sum(r['process_env'].values())
        print(f"\n🟡 process.env 散布 ({total_env} 次，{len(r['process_env'])} 个文件):")
        for fp, cnt in sorted(r['process_env'].items(), key=lambda x: -x[1])[:8]:
            print(f"   {cnt}x  {fp}")
        if not r['process_env']:
            print("   无")

        print_section("超长函数 >50行", len(r['long_funcs']), r['long_funcs'],
                      lambda f: f"{f['lines']}L  {f['file']}::{f['func']} (L{f['start']})")
        print(f"\n🟡 注释掉的代码行: {r['commented']}")
        print_section("已知拼写错误", len(r['typos']), r['typos'],
                      lambda h: f"[{h['typo']}] {h['file']}:{h['line']}")
        print_section("硬编码配置", len(r['hardcoded']), r['hardcoded'],
                      lambda h: f"[{h['label']}] {h['file']}:{h['line']}  {h['content']}")
        print(f"\n📝 JSDoc 覆盖率: {r['jsdoc_rate']}%  (有 {r['has_doc']}，无 {r['no_doc']})")

        print(f"\n{'='*60}\n  汇总\n{'='*60}")
        print(f"  危险调用:              {len(r['dangerous'])} 处")
        if r['is_ts']:
            print(f"  any 类型:              {len(r['any_hits'])} 处")
            print(f"  @ts-ignore:            {len(r['ts_ignore'])} 处")
        print(f"  未处理Promise:         {len(r['unhandled_promise'])} 个文件")
        print(f"  process.env 散布:      {total_env} 次 / {len(r['process_env'])} 文件")
        print(f"  超长函数(>50L):        {len(r['long_funcs'])} 个")
        print(f"  注释代码行:            {r['commented']} 行")
        print(f"  拼写错误:              {len(r['typos'])} 处")
        print(f"  硬编码配置:            {len(r['hardcoded'])} 处")
        print(f"  JSDoc覆盖率:           {r['jsdoc_rate']}%")

    else:  # Python (default) or multi
        r = analyze_python(root, args.focus)
        print(f"📁 扫描 .py 文件数: {r['files']}")

        print_section("危险调用", len(r['dangerous']), r['dangerous'],
                      lambda h: f"[{h['pattern']}] {h['file']}:{h['line']}  {h['content']}")

        total_getenv = sum(r['getenv'].values())
        print(f"\n🟡 os.getenv 散布 ({total_getenv} 次，{len(r['getenv'])} 个文件):")
        for fp, cnt in sorted(r['getenv'].items(), key=lambda x: -x[1])[:10]:
            print(f"   {cnt}x  {fp}")
        if not r['getenv']:
            print("   无")

        print_section("超长函数 >50行", len(r['long_funcs']), r['long_funcs'],
                      lambda f: f"{f['lines']}L  {f['file']}::{f['func']} (L{f['start']})")
        print(f"\n🟡 注释掉的代码行: {r['commented']}")
        print_section("已知拼写错误", len(r['typos']), r['typos'],
                      lambda h: f"[{h['typo']}] {h['file']}:{h['line']}")
        print_section("sys.path.append hack", len(r['path_hacks']), r['path_hacks'],
                      lambda h: f"{h['file']}:{h['line']}")
        print_section("硬编码配置", len(r['hardcoded']), r['hardcoded'],
                      lambda h: f"[{h['label']}] {h['file']}:{h['line']}  {h['content']}")
        print(f"\n📝 类型注解覆盖率: {r['type_ann_rate']}%  (有 {r['has_ann']}，无 {r['no_ann']})")
        print(f"📝 Docstring 覆盖率: {r['docstring_rate']}%  (有 {r['has_doc']}，无 {r['no_doc']})")

        print(f"\n{'='*60}\n  汇总\n{'='*60}")
        print(f"  危险调用:          {len(r['dangerous'])} 处")
        print(f"  os.getenv 散布:    {total_getenv} 次 / {len(r['getenv'])} 文件")
        print(f"  重复文件对:        {len(r['dups'])} 对")
        print(f"  超长函数(>50L):    {len(r['long_funcs'])} 个")
        print(f"  注释代码行:        {r['commented']} 行")
        print(f"  拼写错误:          {len(r['typos'])} 处")
        print(f"  sys.path hack:     {len(r['path_hacks'])} 处")
        print(f"  硬编码配置:        {len(r['hardcoded'])} 处")
        print(f"  类型注解覆盖率:    {r['type_ann_rate']}%")
        print(f"  Docstring覆盖率:   {r['docstring_rate']}%")

    # ── 微服务专项（叠加输出）─────────────────────────────────────────────────
    if info["is_microservice"]:
        ms = analyze_microservice(root, info)
        print(f"\n{'='*60}")
        print("  🏗️  微服务专项")
        print(f"{'='*60}")
        print(f"  检测到服务数: {ms['total_services']}")
        print(f"  Proto 文件:   {len(ms['proto_files'])} 个，分布在 {len(ms['proto_dirs'])} 个目录")
        if ms['proto_dirs']:
            print(f"  Proto 目录:   {', '.join(ms['proto_dirs'][:5])}")

        print(f"\n🔴 硬编码服务地址 ({len(ms['hardcoded_svc'])} 处):")
        for h in ms['hardcoded_svc'][:5]:
            print(f"   {h['file']}:{h['line']}  {h['content']}")
        if not ms['hardcoded_svc']:
            print("   无")

        print(f"\n📝 含 traceId/requestId 的服务: {', '.join(ms['services_with_trace']) or '无'}")
        missing_trace = set(ms['services']) - set(ms['services_with_trace'])
        if missing_trace:
            print(f"   ⚠️  缺少追踪 ID 的服务: {', '.join(sorted(missing_trace)[:5])}")

    print()


if __name__ == "__main__":
    main()
