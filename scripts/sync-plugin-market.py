#!/usr/bin/env python3
"""OTools 插件发布 → 插件市场同步。

流水线中做三件事：

1. 校验插件根目录必须存在 ``logo.svg``（插件市场展示要求）；
2. 把 ``logo.svg`` 拷贝到 otools-website 仓库的 ``public/plugin-logos/<packid>.svg``，
   该仓库的 GitHub Pages 站点会把 logo 发布到 ``<站点根>/plugin-logos/<packid>.svg``；
3. 把插件元信息、``.oplg`` 包地址（GitHub Release 产物）与 logo 线上地址提交给
   xycloud 的插件市场接口：插件不存在则创建，存在则更新。

脚本既用于 GitHub Actions，也可以在本地手工执行（配合 ``--dry-run`` 预演）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_URL = "https://otools-api.lingyun.net/api/v1/otools/plugin/publish"
DEFAULT_WEBSITE_BASE_URL = "https://otools.lingyun.net"
DEFAULT_RELEASE_REPO = "ootools/otools-publish"
LOGO_FILE_NAME = "logo.svg"
LOGO_DIR_NAME = "plugin-logos"


def fail(message: str) -> None:
    print(f"[sync-plugin-market] 错误: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_adapter_root(plugin_dir: Path) -> Path:
    if (plugin_dir / "plugin.json").is_file():
        return plugin_dir
    nested = plugin_dir / "otools"
    if (nested / "plugin.json").is_file():
        return nested
    fail(f"未找到 plugin.json: {plugin_dir}")


def load_manifest(adapter_root: Path) -> dict:
    manifest_path = adapter_root / "plugin.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"plugin.json 解析失败({manifest_path}): {error}")
        return {}


def resolve_logo(plugin_dir: Path, adapter_root: Path) -> Path:
    for candidate in (plugin_dir / LOGO_FILE_NAME, adapter_root / LOGO_FILE_NAME):
        if candidate.is_file():
            return candidate
    fail(
        "插件根目录缺少 logo.svg（插件市场要求每个插件根目录必须提供 logo.svg）: "
        f"{plugin_dir / LOGO_FILE_NAME}"
    )
    return plugin_dir / LOGO_FILE_NAME


def build_package_url(release_repo: str, release_tag: str, packid: str, version: str) -> str:
    asset_name = f"{packid}-{version}.oplg"
    return f"https://github.com/{release_repo}/releases/download/{release_tag}/{asset_name}"


def copy_logo(logo_path: Path, website_dir: Path, packid: str) -> tuple[Path, bool]:
    """把 logo 拷贝到官网仓库，返回 (目标路径, 是否有内容变化)。"""
    target_dir = website_dir / "public" / LOGO_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{packid}.svg"

    source_bytes = logo_path.read_bytes()
    if target.is_file() and target.read_bytes() == source_bytes:
        return target, False
    shutil.copyfile(logo_path, target)
    return target, True


def build_payload(
    manifest: dict,
    packid: str,
    version: str,
    logo_url: str,
    package_url: str,
    official: bool,
) -> dict:
    categories = ["official"] if official else []
    return {
        "packid": packid,
        "uuid": str(manifest.get("uuid") or packid).strip(),
        "displayName": str(manifest.get("displayName") or packid).strip(),
        "displayNameCN": str(manifest.get("displayNameCN") or "").strip(),
        "developerName": str(manifest.get("developerName") or "").strip(),
        "summary": str(manifest.get("summary") or "").strip(),
        "version": version,
        "minOToolsVersion": str(
            manifest.get("minOToolsVersion") or manifest.get("minOtoolsVersion") or ""
        ).strip(),
        "icon": str(manifest.get("icon") or "").strip(),
        "logo": logo_url,
        "entry": str(manifest.get("entry") or "").strip(),
        "categories": categories,
        "screenshots": manifest.get("screenshots") or [],
        "packageUrl": package_url,
        "official": official,
        "supportMacos": True,
        "supportWindows": True,
        "supportLinux": True,
    }


def submit_payload(api_url: str, token: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "X-OTools-Token": token,
            "User-Agent": "otools-publish/sync-plugin-market",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        fail(f"插件市场接口返回 HTTP {error.code}: {detail}")
        return {}
    except urllib.error.URLError as error:
        fail(f"插件市场接口请求失败: {error}")
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fail(f"插件市场接口返回内容无法解析: {raw}")
        return {}

    if int(parsed.get("code") or 0) != 200:
        fail(f"插件市场接口返回异常: {raw}")
    return parsed


def write_github_output(values: dict) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 OTools 插件发布信息到插件市场")
    parser.add_argument("--plugin-dir", required=True, help="插件目录，例如 OTools/plugins/otools-git")
    parser.add_argument("--website-dir", required=True, help="otools-website 仓库检出目录")
    parser.add_argument("--release-repo", default=DEFAULT_RELEASE_REPO, help="发布 .oplg 的 GitHub 仓库")
    parser.add_argument("--release-tag", default="", help="发布 .oplg 的 Release tag")
    parser.add_argument("--packid", default="", help="覆盖 plugin.json 中的 packid")
    parser.add_argument("--version", default="", help="覆盖 plugin.json 中的 version")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="插件市场发布接口地址")
    parser.add_argument("--website-base-url", default=DEFAULT_WEBSITE_BASE_URL, help="官网站点根地址")
    parser.add_argument("--token", default="", help="插件市场发布令牌")
    parser.add_argument("--timeout", type=int, default=30, help="接口超时时间(秒)")
    parser.add_argument("--no-official", action="store_true", help="标记为非官方插件")
    parser.add_argument("--skip-logo", action="store_true", help="只校验 logo，不拷贝到官网仓库")
    parser.add_argument("--skip-submit", action="store_true", help="不调用插件市场接口")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要提交的内容，不拷贝 logo、不调用接口")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    plugin_dir = Path(args.plugin_dir).resolve()
    website_dir = Path(args.website_dir).resolve()
    if not plugin_dir.is_dir():
        fail(f"插件目录不存在: {plugin_dir}")
    if not website_dir.is_dir():
        fail(f"官网仓库目录不存在: {website_dir}")

    adapter_root = resolve_adapter_root(plugin_dir)
    manifest = load_manifest(adapter_root)
    logo_path = resolve_logo(plugin_dir, adapter_root)

    packid = str(args.packid or manifest.get("packid") or "").strip()
    version = str(args.version or manifest.get("version") or "").strip()
    if not packid:
        fail("缺少 packid，请检查 plugin.json 或传入 --packid")
    if not version:
        fail("缺少 version，请检查 plugin.json 或传入 --version")

    release_tag = str(args.release_tag or "").strip() or f"plugin-{packid}-v{version}"
    package_url = build_package_url(args.release_repo, release_tag, packid, version)
    logo_url = f"{args.website_base_url.rstrip('/')}/{LOGO_DIR_NAME}/{packid}.svg"
    official = not args.no_official

    payload = build_payload(manifest, packid, version, logo_url, package_url, official)

    print(f"[sync-plugin-market] 插件: {packid} v{version}")
    print(f"[sync-plugin-market] logo: {logo_path}")
    print(f"[sync-plugin-market] 包地址: {package_url}")
    print(f"[sync-plugin-market] logo 地址: {logo_url}")

    if args.dry_run:
        print("[sync-plugin-market] dry-run 模式，仅输出提交内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    outputs = {
        "packid": packid,
        "version": version,
        "logo_url": logo_url,
        "package_url": package_url,
    }

    if not args.skip_logo:
        target, logo_changed = copy_logo(logo_path, website_dir, packid)
        print(f"[sync-plugin-market] logo 已写入官网仓库: {target} (changed={logo_changed})")
        outputs["logo_changed"] = "true" if logo_changed else "false"
        outputs["logo_path"] = target.as_posix()

    if args.skip_submit:
        write_github_output(outputs)
        print("[sync-plugin-market] 已跳过插件市场接口提交")
        return

    token = str(args.token or "").strip()
    if not token:
        fail("缺少插件市场发布令牌，请通过 --token 或环境变量传入")

    result = submit_payload(args.api_url, token, payload, args.timeout)
    data = result.get("data") or {}
    action = str(data.get("action") or "")
    print(
        "[sync-plugin-market] 插件市场同步完成: "
        f"action={action} id={data.get('id', '')} msg={result.get('msg', '')}"
    )

    outputs["market_action"] = action
    write_github_output(outputs)


if __name__ == "__main__":
    main()
