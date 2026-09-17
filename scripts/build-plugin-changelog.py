#!/usr/bin/env python3
"""为插件发布生成「插件专属」的更新说明。

为什么不用 git log：CI 里 OTools 是 ``git clone --depth 1`` 的浅克隆，
只有最新一条提交，拿不到「上一版以来改了什么」。所以这里改走 GitHub API，
按**插件目录**过滤提交 —— 天然与宿主、与其他插件的提交隔离，
不会把 `feat(dbm): ...` 之类的提交串到别的插件或宿主上。

用法（通常由 build-otools-plugin.yml 调用）：

    python scripts/build-plugin-changelog.py \\
      --plugin-dir otools-dbm --packid otools-dbm --version 0.1.2 \\
      --token "$OTOOLS_TOKEN" --github-output

输出是 markdown 列表（每行一条提交标题）；拿不到任何提交时输出为空，
调用方据此决定要不要写 Release body、要不要同步给市场。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_SOURCE_REPO = "ootools/OTools"  # 插件源码所在仓库
DEFAULT_RELEASE_REPO = "ootools/otools-publish"  # 插件 Release 所在仓库
DEFAULT_PATH_PREFIX = "plugins"
DEFAULT_MAX_COMMITS = 30
DEFAULT_TIMEOUT = 30
MULTILINE_EOF = "CHANGELOG_EOF"


def fail(message: str) -> None:
    """参数/配置类错误：直接失败，需要调用方修正。"""
    print(f"[build-plugin-changelog] 错误: {message}", file=sys.stderr)
    raise SystemExit(1)


def warn(message: str) -> None:
    """接口类问题：更新说明只是增强信息，降级为空说明，不阻断发布。"""
    print(f"[build-plugin-changelog] 警告: {message}", file=sys.stderr)


def plugin_source_path(plugin_dir: str, prefix: str = DEFAULT_PATH_PREFIX) -> str:
    """插件目录在源码仓库里的路径（用于按路径过滤提交）。"""
    normalized = str(plugin_dir or "").strip().strip("/")
    if not normalized:
        return ""
    return f"{prefix}/{normalized}"


def release_tag(packid: str, version: str) -> str:
    return f"plugin-{packid}-v{version}"


def pick_previous_published_at(releases, packid: str, version: str) -> str:
    """取「同一个插件、上一个已发布版本」的发布时间，作为提交筛选起点。

    只认 `plugin-<packid>-v*` 这组 tag，并排除本次正在发布的版本
    （重跑同一版本时，本次的 tag 可能已经存在）。取不到就返回空串，
    调用方据此退化为「最近 N 条提交」。
    """
    prefix = f"plugin-{str(packid or '').strip()}-v"
    current = release_tag(packid, version)
    best = ""
    for item in releases or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not tag.startswith(prefix) or tag == current:
            continue
        published = str(item.get("published_at") or item.get("created_at") or "").strip()
        if not published:
            continue
        if not best or published > best:
            best = published
    return best


def commit_subjects(commits) -> list[str]:
    """把 GitHub commits API 的响应压成提交标题列表（取首行、去空、保序去重）。"""
    subjects: list[str] = []
    for item in commits or []:
        if not isinstance(item, dict):
            continue
        message = str(((item.get("commit") or {}).get("message")) or "").strip()
        if not message:
            continue
        subject = message.splitlines()[0].strip()
        if subject and subject not in subjects:
            subjects.append(subject)
    return subjects


def render_changelog(subjects, max_commits: int = DEFAULT_MAX_COMMITS) -> str:
    """渲染成 markdown 列表；超过上限的提交丢弃。

    注意别写 `max_commits or DEFAULT`：传 0 会被当成假值而退回默认值。
    这里只把 None 视为「未指定」，0 与负数一律收敛到 1（至少留一条）。
    """
    limit = DEFAULT_MAX_COMMITS if max_commits is None else max(1, int(max_commits))
    picked = list(subjects or [])[:limit]
    return "\n".join(f"- {subject}" for subject in picked)


def build_commits_url(
    api_base: str, repo: str, path: str, since: str, per_page: int = 100
) -> str:
    query = {"path": path, "per_page": str(per_page)}
    if since:
        query["since"] = since
    return f"{api_base.rstrip('/')}/repos/{repo}/commits?{urllib.parse.urlencode(query)}"


def fetch_json(url: str, token: str, timeout: int = DEFAULT_TIMEOUT):
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "otools-publish/build-plugin-changelog",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        warn(f"GitHub API 返回 HTTP {error.code}: {detail[:200]}")
        return None
    except urllib.error.URLError as error:
        warn(f"GitHub API 请求失败: {error}")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        warn(f"GitHub API 返回内容无法解析: {raw[:200]}")
        return None
    if not isinstance(parsed, list):
        warn("GitHub API 返回内容不是数组")
        return None
    return parsed


def write_github_output(values: dict) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = str(value or "")
            if "\n" in text:
                handle.write(f"{key}<<{MULTILINE_EOF}\n{text}\n{MULTILINE_EOF}\n")
            else:
                handle.write(f"{key}={text}\n")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成插件专属的更新说明")
    parser.add_argument("--plugin-dir", required=True, help="插件目录名，例如 otools-dbm")
    parser.add_argument("--packid", default="", help="插件 packid，默认与 --plugin-dir 相同")
    parser.add_argument("--version", default="", help="本次发布的版本号")
    parser.add_argument("--source-repo", default=DEFAULT_SOURCE_REPO, help="插件源码仓库 owner/repo")
    parser.add_argument("--release-repo", default=DEFAULT_RELEASE_REPO, help="插件 Release 所在仓库 owner/repo")
    parser.add_argument("--path-prefix", default=DEFAULT_PATH_PREFIX, help="源码仓库里插件的根目录前缀")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="GitHub API 根地址")
    parser.add_argument("--token", default="", help="GitHub 令牌（读私有仓库时必需）")
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS, help="最多收录多少条提交")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="接口超时(秒)")
    parser.add_argument("--output", default="", help="额外写入到该文件")
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="把结果写入 GITHUB_OUTPUT（供后续步骤读取）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要生成的内容，不请求接口")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)

    plugin_dir = str(args.plugin_dir or "").strip()
    if not plugin_dir:
        fail("缺少 --plugin-dir")
    packid = str(args.packid or "").strip() or plugin_dir
    version = str(args.version or "").strip()
    path = plugin_source_path(plugin_dir, args.path_prefix)

    if args.dry_run:
        print(f"[build-plugin-changelog] 将按路径 {path} 过滤提交（dry-run，未请求接口）")
        return

    since = ""
    if version:
        releases = fetch_json(
            f"{args.api_base.rstrip('/')}/repos/{args.release_repo}/releases?per_page=100",
            args.token,
            args.timeout,
        )
        since = pick_previous_published_at(releases, packid, version)
        if since:
            print(f"[build-plugin-changelog] 上一版发布于 {since}，取其之后的提交")
        else:
            print("[build-plugin-changelog] 未找到同插件的历史版本，退化为最近的提交")
    else:
        print("[build-plugin-changelog] 未提供版本号，退化为最近的提交")

    commits = fetch_json(
        build_commits_url(args.api_base, args.source_repo, path, since),
        args.token,
        args.timeout,
    )
    subjects = commit_subjects(commits)
    changelog = render_changelog(subjects, args.max_commits)

    print(f"[build-plugin-changelog] 插件: {packid} v{version or '-'}")
    print(f"[build-plugin-changelog] 路径: {path}")
    print(f"[build-plugin-changelog] 命中提交: {len(subjects)} 条（收录 {min(len(subjects), max(1, args.max_commits))} 条）")

    if args.output:
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(changelog, encoding="utf-8")
        print(f"[build-plugin-changelog] 已写入: {target}")

    if args.github_output:
        write_github_output({"changelog": changelog, "commit_count": str(len(subjects))})
    else:
        print(changelog)


if __name__ == "__main__":
    main()
