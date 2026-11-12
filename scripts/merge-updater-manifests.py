#!/usr/bin/env python3
"""把多个平台各自生成的 latest.json 合并成一份完整清单，并校验资产真实存在。

发布工作流里每个平台 job 各自上传一份 latest.json，最后一个会覆盖前面的，
导致 release 上的清单只剩一个平台。这个脚本负责在全部平台构建完成后合并。

用法：
    python3 merge-manifests.py --out /tmp/latest.json \
        /tmp/otools-manifests/macos-arm/updater-manifest/latest-macos-latest.json \
        /tmp/otools-manifests/macos-intel/updater-manifest/latest-macos-15-intel.json \
        /tmp/otools-manifests/windows/updater-manifest/latest-windows-2022.json \
        /tmp/otools-manifests/linux/updater-manifest/latest-ubuntu-24.04.json
"""

import argparse
import json
import re
import subprocess
import sys

REPO = "ootools/otools-publish"


def release_created_at(tag):
    out = subprocess.check_output(
        ["gh", "api", f"repos/{REPO}/releases?per_page=20"], text=True
    )
    for r in json.loads(out):
        if r["tag_name"] == tag:
            return r.get("published_at") or r.get("created_at")
    sys.exit(f"找不到 release {tag}")


def release_assets(tag):
    out = subprocess.check_output(
        ["gh", "api", f"repos/{REPO}/releases?per_page=20"], text=True
    )
    rid = next((r["id"] for r in json.loads(out) if r["tag_name"] == tag), None)
    if not rid:
        sys.exit(f"找不到 release {tag}")
    out = subprocess.check_output(
        ["gh", "api", f"repos/{REPO}/releases/{rid}/assets?per_page=200"], text=True
    )
    return {a["name"]: a["size"] for a in json.loads(out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifests", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tag", default="v0.9.0")
    ap.add_argument("--skip-asset-check", action="store_true")
    args = ap.parse_args()

    merged = {"version": None, "notes": "", "pub_date": None, "platforms": {}}
    seen = {}

    for path in args.manifests:
        data = json.load(open(path, encoding="utf-8"))
        version = data.get("version")
        if merged["version"] and version and version != merged["version"]:
            sys.exit(f"版本不一致：{path} -> {version}，已合并 {merged['version']}")
        merged["version"] = merged["version"] or version
        merged["pub_date"] = merged["pub_date"] or data.get("pub_date")
        if data.get("notes"):
            merged["notes"] = data["notes"]
        for key, value in (data.get("platforms") or {}).items():
            if key in seen:
                print(f"  跳过重复平台 {key}（来自 {path}，已有 {seen[key]}）")
                continue
            merged["platforms"][key] = value
            seen[key] = path

    for key, value in merged["platforms"].items():
        if not value.get("url") or not value.get("signature"):
            sys.exit(f"平台 {key} 缺少 url 或 signature")

    # 各平台 job 生成的清单里没有 pub_date，这里补成 release 的创建时间，
    # 免得客户端把「没有日期」当成异常清单。
    if not merged.get("pub_date"):
        merged["pub_date"] = release_created_at(args.tag)

    if not args.skip_asset_check:
        assets = release_assets(args.tag)
        for key, value in merged["platforms"].items():
            name = value["url"].rsplit("/", 1)[-1]
            if name not in assets:
                sys.exit(f"平台 {key} 指向的资产 {name} 不在 release {args.tag} 中")
            if not re.match(rf"^https://github\.com/{REPO}/releases/download/{args.tag}/", value["url"]):
                sys.exit(f"平台 {key} 的 URL 前缀异常：{value['url']}")
            print(f"  ✓ {key:24} -> {name} ({assets[name] // 1024} KiB)")

    json.dump(merged, open(args.out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"合并完成：{len(merged['platforms'])} 个平台 -> {args.out}")
    print("  " + ", ".join(sorted(merged["platforms"])))


if __name__ == "__main__":
    main()
