#!/usr/bin/env python3
"""为宿主(OTools)发布生成更新说明。

为什么不用 git log：CI 里 OTools 是 ``git clone --depth 1`` 的浅克隆，
只有最新一条提交，拿不到「上一版以来改了什么」。所以这里改走 GitHub API，
取「上一个宿主版本发布以来」的全部提交作为更新说明。

宿主与插件的发布隔离：插件走 ``build-plugin-changelog.py``（按 ``plugins/<目录>``
过滤，只列本插件改动），宿主这里不过滤路径，列出宿主仓库自上一版以来的全部提交
（含被一起打包进宿主的改动）。两条流水线各出各的说明，互不串扰。

用法（由 build-otools.yml 的 publish-updater-manifest job 调用）：

    python scripts/build-host-changelog.py \\
      --version 0.7.9 --token "$OTOOLS_TOKEN" --github-output --output changelog.txt

输出是 markdown（标题 + 列表，每行一条提交标题）；拿不到任何提交时输出兜底说明，
调用方据此决定要不要写 Release body / latest.json notes。脚本保证始终写出
一个非空文件（即使接口失败也写兜底内容），避免下游 body_path 因文件缺失而报错。
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
DEFAULT_SOURCE_REPO = "ootools/OTools"        # 宿主源码所在仓库
DEFAULT_RELEASE_REPO = "ootools/otools-publish"  # 宿主 Release 所在仓库（latest.json 也在这里）
DEFAULT_MAX_COMMITS = 30
DEFAULT_TIMEOUT = 30
MULTILINE_EOF = "CHANGELOG_EOF"


def fail(message: str) -> None:
    print(f"[build-host-changelog] 错误: {message}", file=sys.stderr)
    raise SystemExit(1)


def warn(message: str) -> None:
    print(f"[build-host-changelog] 警告: {message}", file=sys.stderr)


def pick_previous_host_published_at(releases, version: str) -> str:
    """取「上一个已发布的宿主版本」的发布时间，作为提交筛选起点。

    宿主 Release 的 tag 形如 ``vX.Y.Z``；插件 Release 的 tag 形如
    ``plugin-<packid>-vX.Y.Z``，必须排除。同时排除本次正在发布的版本
    （重跑同一版本时，本次的 tag 可能已经存在）。取不到就返回空串，
    调用方据此退化为「最近 N 条提交」。
    """
    current = f"v{version}"
    best = ""
    for item in releases or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not tag.startswith("v") or tag.startswith("plugin-") or tag == current:
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


def render_changelog(version: str, subjects, max_commits: int = DEFAULT_MAX_COMMITS) -> str:
    """渲染成 markdown（标题 + 列表）；超过上限的提交丢弃。

    注意别写 ``max_commits or DEFAULT``：传 0 会被当成假值而退回默认值。
    这里只把 None 视为「未指定」，0 与负数一律收敛到 1（至少留一条）。
    """
    limit = DEFAULT_MAX_COMMITS if max_commits is None else max(1, int(max_commits))
    picked = list(subjects or [])[:limit]
    if not picked:
        return f"OTools v{version} 更新内容：\n\n- 本次更新包含若干稳定性与体验优化。"
    lines = "\n".join(f"- {subject}" for subject in picked)
    return f"OTools v{version} 更新内容：\n\n{lines}"


def build_commits_url(api_base: str, repo: str, since: str, per_page: int = 100) -> str:
    query = {"per_page": str(per_page)}
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
            "User-Agent": "otools-publish/build-host-changelog",
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
    parser = argparse.ArgumentParser(description="生成宿主(OTools)的更新说明")
    parser.add_argument("--version", default="", help="本次发布的宿主版本号，例如 0.7.9")
    parser.add_argument("--source-repo", default=DEFAULT_SOURCE_REPO, help="宿主源码仓库 owner/repo")
    parser.add_argument("--release-repo", default=DEFAULT_RELEASE_REPO, help="宿主 Release 所在仓库 owner/repo")
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


def build_text(version: str, token: str, args: argparse.Namespace) -> str:
    version = str(version or "").strip()
    since = ""
    if version:
        releases = fetch_json(
            f"{args.api_base.rstrip('/')}/repos/{args.release_repo}/releases?per_page=100",
            token,
            args.timeout,
        )
        since = pick_previous_host_published_at(releases, version)
        if since:
            print(f"[build-host-changelog] 上一版宿主发布于 {since}，取其之后的提交")
        else:
            print("[build-host-changelog] 未找到历史宿主版本，退化为最近的提交")
    else:
        print("[build-host-changelog] 未提供版本号，退化为最近的提交")

    commits = fetch_json(
        build_commits_url(args.api_base, args.source_repo, since),
        token,
        args.timeout,
    )
    subjects = commit_subjects(commits)
    return render_changelog(version, subjects, args.max_commits)


def main(argv=None) -> None:
    args = parse_args(argv)
    version = str(args.version or "").strip()

    if args.dry_run:
        print(f"[build-host-changelog] 将生成宿主 v{version} 的更新说明（dry-run，未请求接口）")
        return

    # 兜底：任何异常都写出非空内容，避免下游 body_path 因文件缺失而报错。
    try:
        text = build_text(version, args.token, args)
    except Exception as exc:  # noqa: BLE001 - 更新说明只是增强信息，降级不阻断发布
        warn(f"生成更新说明失败，使用兜底内容: {exc}")
        text = render_changelog(version, [], args.max_commits)

    print(f"[build-host-changelog] 宿主: v{version or '-'}")
    print(f"[build-host-changelog] 更新说明长度: {len(text)} 字符")

    if args.output:
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"[build-host-changelog] 已写入: {target}")

    if args.github_output:
        write_github_output({"changelog": text, "commit_count": str(len(text))})
    else:
        print(text)


if __name__ == "__main__":
    main()
