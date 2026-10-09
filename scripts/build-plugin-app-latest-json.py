#!/usr/bin/env python3
"""为「插件独立 APP」生成 Tauri 更新清单（latest.json）。

与宿主 `build-otools.yml` 的做法同源，但有一处关键差异：

- 宿主：清单里的资产地址指向 **版本化 tag** `v<version>`，端点用
  `.../releases/latest/download/latest.json`；
- 插件 APP：清单里的资产地址同样指向版本化 tag `plugin-app-<packid>-v<version>`，
  但端点必须是**该插件专属的固定 tag** `plugin-app-<packid>` ——
  不能用 `/releases/latest/`，因为 GitHub 的 latest = 「最新非草稿非预发布 release」，
  插件发得比宿主晚时 latest 会落到插件 release 上，它没有 latest.json，
  于是所有宿主客户端的「检查更新」都会失败。

因此本脚本有两个子命令：

    # 各平台 job：按平台产出片段清单（platforms 里只放本平台能用的键）
    python scripts/build-plugin-app-latest-json.py platform \\
      --plugin-dir plugins/otools-git --version 0.2.6 \\
      --artifact-dir dist-artifacts --platform macos-latest \\
      --release-repo ootools/otools-publish \\
      --artifact-tag plugin-app-otools-git-v0.2.6 \\
      --output platform-latest.json

    # 汇总 job：把各平台片段合并成最终 latest.json
    python scripts/build-plugin-app-latest-json.py merge \\
      --manifest-dir manifests --version 0.2.6 --notes "$CHANGELOG" \\
      --output latest.json

产物里的 `platforms` 键与 Tauri updater 的约定一致：
`<os>-<arch>` 指向首选包，`<os>-<arch>-<bundle>` 指向各具体包型。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RELEASE_REPO = "ootools/otools-publish"
DEFAULT_NOTES = "See the assets to download this version and install."
MULTILINE_EOF = "LATEST_JSON_EOF"

# GitHub runner 平台 → Tauri updater 的 os 键
OS_KEY_BY_PLATFORM = {
    "macos-latest": "darwin",
    "macos-15-intel": "darwin",
    "macos-14": "darwin",
    "ubuntu-latest": "linux",
    "ubuntu-22.04": "linux",
    "windows-latest": "windows",
}

# 各 os 上「能被 updater 直接安装」的包型，以及优先级（越小越优先）
ALLOWED_BUNDLES = {
    "linux": ("appimage", "deb", "rpm"),
    "darwin": ("app",),
    "windows": ("nsis", "msi"),
}

ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "i686": "i686",
    "i386": "i686",
    "armv7l": "armv7",
}


def fail(message: str) -> None:
    print(f"[build-plugin-app-latest-json] 错误: {message}", file=sys.stderr)
    raise SystemExit(1)


def warn(message: str) -> None:
    print(f"[build-plugin-app-latest-json] 警告: {message}", file=sys.stderr)


def ensure_utf8_output() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 上 Python 默认用 cp1252 输出，打印中文（本脚本所有提示都带中文）
    会直接抛 UnicodeEncodeError。宿主与 `.oplg` 的脚本只在 ubuntu 上跑，没暴露过；
    独立 APP 的清单生成要在 Windows runner 上跑，必须自己兜住。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def resolve_os_key(platform: str) -> str:
    """把 GitHub runner 平台名映射成 updater 的 os 键；未知平台回退 linux。"""
    normalized = str(platform or "").strip()
    if normalized in OS_KEY_BY_PLATFORM:
        return OS_KEY_BY_PLATFORM[normalized]
    if normalized.startswith("macos"):
        return "darwin"
    if normalized.startswith("windows"):
        return "windows"
    return "linux"


def resolve_arch(machine: str) -> str:
    """把 `platform.machine()` 的取值收敛到 updater 认的四种架构之一。"""
    normalized = str(machine or "").strip().lower()
    arch = ARCH_ALIASES.get(normalized, normalized)
    if arch not in {"x86_64", "aarch64", "i686", "armv7"}:
        return "x86_64"
    return arch


def detect_bundle(file_name: str) -> str:
    """从文件名推断包型；无法识别返回空串。"""
    lower = str(file_name or "").lower()
    if lower.endswith(".app.tar.gz"):
        return "app"
    if lower.endswith(".appimage") or lower.endswith(".appimage.tar.gz"):
        return "appimage"
    if lower.endswith(".deb"):
        return "deb"
    if lower.endswith(".rpm"):
        return "rpm"
    if lower.endswith(".msi"):
        return "msi"
    if lower.endswith(".exe"):
        return "nsis"
    return ""


def collect_candidates(sig_paths, os_key: str):
    """把 `.sig` 文件配成 (包型, 资产文件名, 签名) 三元组，并筛掉本平台用不了的包型。

    跳过规则与宿主一致：签名文件为空、资产本体不存在、包型不被 updater 支持，都跳过。
    """
    allowed = ALLOWED_BUNDLES.get(os_key, ())
    candidates = []
    for sig_path in sorted(sig_paths or []):
        artifact_path = sig_path[: -len(".sig")] if sig_path.endswith(".sig") else ""
        if not artifact_path or not os.path.isfile(artifact_path):
            continue
        bundle = detect_bundle(artifact_path)
        if not bundle or bundle not in allowed:
            continue
        try:
            signature = Path(sig_path).read_text(encoding="utf-8").strip()
        except OSError as error:
            warn(f"读取签名失败，跳过 {sig_path}: {error}")
            continue
        if not signature:
            continue
        candidates.append(
            {
                "bundle": bundle,
                "basename": os.path.basename(artifact_path),
                "signature": signature,
            }
        )
    return candidates


def build_platform_manifest(
    candidates,
    *,
    version: str,
    os_key: str,
    arch: str,
    release_repo: str,
    artifact_tag: str,
    notes: str = DEFAULT_NOTES,
) -> dict:
    """把候选包编排成平台片段清单。

    首选包（按 ALLOWED_BUNDLES 的优先级）同时占用 `<os>-<arch>` 这个通用键，
    其余包型只占 `<os>-<arch>-<bundle>`，与 Tauri updater 的键约定一致。
    """
    if not candidates:
        fail(f"{os_key}-{arch} 没有可用的「资产 + 签名」配对")

    order = {name: index for index, name in enumerate(ALLOWED_BUNDLES.get(os_key, ()))}
    ordered = sorted(candidates, key=lambda item: order.get(item["bundle"], 999))

    platforms = {}
    for index, item in enumerate(ordered):
        # 资产名要做 URL 编码：productName 里可能有空格（如 `OTools Git_0.2.6_aarch64.dmg`），
        # 直接拼进 URL 会拿到 404。
        asset_name = urllib.parse.quote(str(item["basename"]), safe="")
        url = f"https://github.com/{release_repo}/releases/download/{artifact_tag}/{asset_name}"
        entry = {"signature": item["signature"], "url": url}
        if index == 0:
            platforms[f"{os_key}-{arch}"] = dict(entry)
        platforms[f"{os_key}-{arch}-{item['bundle']}"] = dict(entry)

    return {
        "version": str(version or "").lstrip("v"),
        "notes": notes or DEFAULT_NOTES,
        "platforms": platforms,
    }


def merge_manifests(manifests, *, version: str, notes: str = "", pub_date: str = "") -> dict:
    """合并各平台片段清单。

    与宿主一致：同名键**先到先得**（`setdefault`），避免后面的平台覆盖前面已经
    验证过的条目；版本号不一致的片段直接丢弃（防止串版本）。
    """
    expected = str(version or "").lstrip("v")
    merged: dict = {}
    merged_notes = ""
    merged_pub_date = ""

    for item in manifests or []:
        if not isinstance(item, dict):
            continue
        manifest_version = str(item.get("version", "")).strip().lstrip("v")
        if expected and manifest_version and manifest_version != expected:
            warn(f"跳过版本不匹配的片段: {manifest_version} != {expected}")
            continue
        platforms = item.get("platforms")
        if not isinstance(platforms, dict) or not platforms:
            continue
        for key, value in platforms.items():
            if isinstance(value, dict) and value.get("url") and value.get("signature"):
                merged.setdefault(key, value)
        if not merged_notes and item.get("notes"):
            merged_notes = str(item["notes"])
        if not merged_pub_date and item.get("pub_date"):
            merged_pub_date = str(item["pub_date"])

    if not merged:
        fail("合并后没有任何有效的平台条目")

    return {
        "version": expected,
        "notes": notes or merged_notes or DEFAULT_NOTES,
        "pub_date": pub_date or merged_pub_date or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": dict(sorted(merged.items(), key=lambda pair: pair[0])),
    }


def load_manifests(manifest_dir: str):
    """读取目录下所有 `latest*.json` 片段（忽略解析失败的）。"""
    manifests = []
    pattern = os.path.join(str(manifest_dir or ""), "**", "*.json")
    for file_path in sorted(set(glob.glob(pattern, recursive=True))):
        name = os.path.basename(file_path).lower()
        if not (name.startswith("latest") and name.endswith(".json")):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            warn(f"跳过无法解析的片段 {file_path}: {error}")
            continue
        if isinstance(data, dict):
            manifests.append(data)
    return manifests


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
    parser = argparse.ArgumentParser(description="生成插件独立 APP 的 Tauri 更新清单")
    subparsers = parser.add_subparsers(dest="command", required=True)

    platform = subparsers.add_parser("platform", help="按平台产出片段清单")
    platform.add_argument("--version", required=True, help="本次发布的版本号")
    platform.add_argument("--artifact-dir", required=True, help="构建产物目录（含 .sig）")
    platform.add_argument("--platform", required=True, help="GitHub runner 平台名，例如 macos-latest")
    platform.add_argument("--arch", default="", help="覆盖架构推断（x86_64 / aarch64 / …）")
    platform.add_argument("--release-repo", default=DEFAULT_RELEASE_REPO, help="Release 所在仓库 owner/repo")
    platform.add_argument("--artifact-tag", required=True, help="安装包所在的版本化 tag")
    platform.add_argument("--notes", default=DEFAULT_NOTES, help="更新说明")
    platform.add_argument("--output", required=True, help="写出的片段清单路径")

    merge = subparsers.add_parser("merge", help="合并各平台片段清单")
    merge.add_argument("--manifest-dir", required=True, help="片段清单所在目录")
    merge.add_argument("--version", required=True, help="本次发布的版本号")
    merge.add_argument("--notes", default="", help="更新说明（优先于片段里的 notes）")
    merge.add_argument("--pub-date", default="", help="发布时间（默认取当前 UTC 时间）")
    merge.add_argument("--output", required=True, help="写出的 latest.json 路径")

    for item in (platform, merge):
        item.add_argument("--github-output", action="store_true", help="把结果写入 GITHUB_OUTPUT")

    return parser.parse_args(argv)


def main(argv=None) -> None:
    ensure_utf8_output()
    args = parse_args(argv)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.command == "platform":
        import platform as platform_module

        os_key = resolve_os_key(args.platform)
        arch = str(args.arch or "").strip() or resolve_arch(platform_module.machine())
        sig_paths = glob.glob(os.path.join(str(args.artifact_dir), "**", "*.sig"), recursive=True)
        candidates = collect_candidates(sig_paths, os_key)
        manifest = build_platform_manifest(
            candidates,
            version=args.version,
            os_key=os_key,
            arch=arch,
            release_repo=args.release_repo,
            artifact_tag=args.artifact_tag,
            notes=args.notes,
        )
        output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"[build-plugin-app-latest-json] {os_key}-{arch}: "
            f"{len(candidates)} 个候选包 → {output_path}"
        )
    else:
        manifests = load_manifests(args.manifest_dir)
        merged = merge_manifests(manifests, version=args.version, notes=args.notes, pub_date=args.pub_date)
        output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            f"[build-plugin-app-latest-json] 合并 {len(manifests)} 个片段 → "
            f"{len(merged['platforms'])} 个平台条目 → {output_path}"
        )

    if args.github_output:
        write_github_output({"manifest_path": str(output_path)})


if __name__ == "__main__":
    main()
