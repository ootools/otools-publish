"""build-plugin-app-latest-json.py 的单元测试。

脚本文件名带连字符，无法直接 import，这里用 importlib 按路径加载。
运行：python tests/test_build_plugin_app_latest_json.py
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-plugin-app-latest-json.py"

spec = importlib.util.spec_from_file_location("build_plugin_app_latest_json", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_pair(directory: Path, name: str, signature: str = "sig-value") -> Path:
    """在目录里造一个「资产 + 签名」配对，返回签名文件路径。"""
    artifact = directory / name
    artifact.write_bytes(b"binary")
    sig = directory / f"{name}.sig"
    sig.write_text(signature, encoding="utf-8")
    return sig


class ResolveOsKeyTests(unittest.TestCase):
    def test_known_platforms(self) -> None:
        self.assertEqual(module.resolve_os_key("macos-latest"), "darwin")
        self.assertEqual(module.resolve_os_key("macos-15-intel"), "darwin")
        self.assertEqual(module.resolve_os_key("ubuntu-latest"), "linux")
        self.assertEqual(module.resolve_os_key("windows-latest"), "windows")

    def test_prefix_fallback(self) -> None:
        self.assertEqual(module.resolve_os_key("macos-99"), "darwin")
        self.assertEqual(module.resolve_os_key("windows-2025"), "windows")

    def test_unknown_falls_back_to_linux(self) -> None:
        self.assertEqual(module.resolve_os_key(""), "linux")
        self.assertEqual(module.resolve_os_key("freebsd-latest"), "linux")


class ResolveArchTests(unittest.TestCase):
    def test_aliases(self) -> None:
        self.assertEqual(module.resolve_arch("AMD64"), "x86_64")
        self.assertEqual(module.resolve_arch("arm64"), "aarch64")
        self.assertEqual(module.resolve_arch("aarch64"), "aarch64")

    def test_unsupported_falls_back_to_x86_64(self) -> None:
        self.assertEqual(module.resolve_arch("mips"), "x86_64")
        self.assertEqual(module.resolve_arch(""), "x86_64")


class DetectBundleTests(unittest.TestCase):
    def test_known_bundles(self) -> None:
        self.assertEqual(module.detect_bundle("app.app.tar.gz"), "app")
        self.assertEqual(module.detect_bundle("app_0.1.0_amd64.AppImage"), "appimage")
        self.assertEqual(module.detect_bundle("app_0.1.0_amd64.deb"), "deb")
        self.assertEqual(module.detect_bundle("app-0.1.0-1.x86_64.rpm"), "rpm")
        self.assertEqual(module.detect_bundle("app_0.1.0_x64-setup.exe"), "nsis")
        self.assertEqual(module.detect_bundle("app_0.1.0_x64_en-US.msi"), "msi")

    def test_dmg_is_not_updatable(self) -> None:
        self.assertEqual(module.detect_bundle("app_0.1.0_aarch64.dmg"), "")

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(module.detect_bundle(""), "")
        self.assertEqual(module.detect_bundle("readme.txt"), "")


class CollectCandidatesTests(unittest.TestCase):
    def test_filters_by_platform_allowed_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pair(root, "app.app.tar.gz")
            write_pair(root, "app_0.1.0_amd64.AppImage")
            write_pair(root, "app_0.1.0_aarch64.dmg")

            sigs = [str(path) for path in root.glob("*.sig")]
            darwin = module.collect_candidates(sigs, "darwin")
            linux = module.collect_candidates(sigs, "linux")

        self.assertEqual([item["bundle"] for item in darwin], ["app"])
        self.assertEqual([item["bundle"] for item in linux], ["appimage"])

    def test_skips_missing_artifact_and_empty_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pair(root, "app.app.tar.gz")
            write_pair(root, "ghost.app.tar.gz", signature="")
            (root / "ghost.app.tar.gz").unlink()
            write_pair(root, "empty.deb", signature="   ")

            sigs = [str(path) for path in root.glob("*.sig")]
            candidates = module.collect_candidates(sigs, "darwin")

        self.assertEqual([item["basename"] for item in candidates], ["app.app.tar.gz"])


class BuildPlatformManifestTests(unittest.TestCase):
    def test_preferred_bundle_also_takes_generic_key(self) -> None:
        candidates = [
            {"bundle": "deb", "basename": "app.deb", "signature": "s-deb"},
            {"bundle": "appimage", "basename": "app.AppImage", "signature": "s-img"},
        ]
        manifest = module.build_platform_manifest(
            candidates,
            version="0.2.6",
            os_key="linux",
            arch="x86_64",
            release_repo="ootools/otools-publish",
            artifact_tag="plugin-app-otools-git-v0.2.6",
        )

        # appimage 优先级最高 → 同时占用通用键
        self.assertEqual(manifest["platforms"]["linux-x86_64"]["signature"], "s-img")
        self.assertEqual(
            manifest["platforms"]["linux-x86_64-appimage"]["url"],
            "https://github.com/ootools/otools-publish/releases/download/"
            "plugin-app-otools-git-v0.2.6/app.AppImage",
        )
        self.assertEqual(manifest["platforms"]["linux-x86_64-deb"]["signature"], "s-deb")
        self.assertEqual(manifest["version"], "0.2.6")

    def test_version_strips_leading_v(self) -> None:
        manifest = module.build_platform_manifest(
            [{"bundle": "app", "basename": "a.app.tar.gz", "signature": "s"}],
            version="v0.2.6",
            os_key="darwin",
            arch="aarch64",
            release_repo="r/r",
            artifact_tag="t",
        )
        self.assertEqual(manifest["version"], "0.2.6")
        self.assertEqual(sorted(manifest["platforms"]), ["darwin-aarch64", "darwin-aarch64-app"])

    def test_asset_name_is_url_encoded(self) -> None:
        """productName 含空格时，安装包名里会带空格，URL 必须编码，否则 404。"""
        manifest = module.build_platform_manifest(
            [{"bundle": "app", "basename": "OTools Git_0.2.6_aarch64.app.tar.gz", "signature": "s"}],
            version="0.2.6",
            os_key="darwin",
            arch="aarch64",
            release_repo="r/r",
            artifact_tag="t",
        )
        url = manifest["platforms"]["darwin-aarch64"]["url"]
        self.assertIn("OTools%20Git_0.2.6_aarch64.app.tar.gz", url)
        self.assertNotIn(" ", url)

    def test_empty_candidates_fails(self) -> None:
        with self.assertRaises(SystemExit):
            module.build_platform_manifest(
                [],
                version="0.2.6",
                os_key="darwin",
                arch="aarch64",
                release_repo="r/r",
                artifact_tag="t",
            )


class MergeManifestsTests(unittest.TestCase):
    def test_first_wins_for_duplicate_keys(self) -> None:
        first = {
            "version": "0.2.6",
            "platforms": {"darwin-aarch64": {"url": "u1", "signature": "s1"}},
        }
        second = {
            "version": "0.2.6",
            "platforms": {
                "darwin-aarch64": {"url": "u2", "signature": "s2"},
                "linux-x86_64": {"url": "u3", "signature": "s3"},
            },
        }
        merged = module.merge_manifests([first, second], version="0.2.6")

        self.assertEqual(merged["platforms"]["darwin-aarch64"]["url"], "u1")
        self.assertEqual(merged["platforms"]["linux-x86_64"]["url"], "u3")
        self.assertEqual(sorted(merged["platforms"]), ["darwin-aarch64", "linux-x86_64"])

    def test_skips_version_mismatch(self) -> None:
        good = {"version": "0.2.6", "platforms": {"darwin-aarch64": {"url": "u", "signature": "s"}}}
        stale = {"version": "0.2.5", "platforms": {"windows-x86_64": {"url": "x", "signature": "y"}}}
        merged = module.merge_manifests([good, stale], version="0.2.6")

        self.assertEqual(sorted(merged["platforms"]), ["darwin-aarch64"])

    def test_drops_entries_without_url_or_signature(self) -> None:
        broken = {
            "version": "0.2.6",
            "platforms": {
                "darwin-aarch64": {"url": "u", "signature": "s"},
                "linux-x86_64": {"url": "", "signature": "s"},
                "windows-x86_64": {"url": "u"},
            },
        }
        merged = module.merge_manifests([broken], version="0.2.6")

        self.assertEqual(sorted(merged["platforms"]), ["darwin-aarch64"])

    def test_notes_and_pub_date_precedence(self) -> None:
        manifest = {
            "version": "0.2.6",
            "notes": "片段里的说明",
            "pub_date": "2026-01-01T00:00:00Z",
            "platforms": {"darwin-aarch64": {"url": "u", "signature": "s"}},
        }
        explicit = module.merge_manifests([manifest], version="0.2.6", notes="显式说明", pub_date="")
        self.assertEqual(explicit["notes"], "显式说明")
        self.assertEqual(explicit["pub_date"], "2026-01-01T00:00:00Z")

        fallback = module.merge_manifests([manifest], version="0.2.6")
        self.assertEqual(fallback["notes"], "片段里的说明")

    def test_empty_fails(self) -> None:
        with self.assertRaises(SystemExit):
            module.merge_manifests([], version="0.2.6")


class LoadManifestsTests(unittest.TestCase):
    def test_reads_latest_json_recursively_and_skips_broken(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a").mkdir()
            (root / "a" / "latest-macos-latest.json").write_text(
                json.dumps({"version": "0.2.6", "platforms": {"darwin-aarch64": {"url": "u", "signature": "s"}}}),
                encoding="utf-8",
            )
            (root / "other.json").write_text("{}", encoding="utf-8")
            (root / "latest-broken.json").write_text("{not json", encoding="utf-8")

            manifests = module.load_manifests(str(root))

        self.assertEqual(len(manifests), 1)
        self.assertIn("darwin-aarch64", manifests[0]["platforms"])


class GithubOutputTests(unittest.TestCase):
    def test_writes_single_line_and_multiline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output_path = Path(temp) / "out.txt"
            previous = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output_path)
            try:
                module.write_github_output({"manifest_path": "/tmp/latest.json", "notes": "a\nb"})
            finally:
                if previous is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = previous

            text = output_path.read_text(encoding="utf-8")

        self.assertIn("manifest_path=/tmp/latest.json\n", text)
        self.assertIn("notes<<LATEST_JSON_EOF\n", text)

    def test_no_env_is_noop(self) -> None:
        previous = os.environ.pop("GITHUB_OUTPUT", None)
        try:
            module.write_github_output({"a": "b"})
        finally:
            if previous is not None:
                os.environ["GITHUB_OUTPUT"] = previous


class EnsureUtf8OutputTests(unittest.TestCase):
    """Windows runner 的 Python 默认是 cp1252，打印中文会 UnicodeEncodeError。"""

    def test_reconfigures_both_streams(self) -> None:
        calls = []

        class FakeStream:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        original_out, original_err = module.sys.stdout, module.sys.stderr
        module.sys.stdout, module.sys.stderr = FakeStream(), FakeStream()
        try:
            module.ensure_utf8_output()
        finally:
            module.sys.stdout, module.sys.stderr = original_out, original_err

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item.get("encoding") == "utf-8" for item in calls))

    def test_tolerates_streams_without_reconfigure(self) -> None:
        class LegacyStream:
            pass

        original_out, original_err = module.sys.stdout, module.sys.stderr
        module.sys.stdout, module.sys.stderr = LegacyStream(), LegacyStream()
        try:
            module.ensure_utf8_output()  # 不应抛异常
        finally:
            module.sys.stdout, module.sys.stderr = original_out, original_err


if __name__ == "__main__":
    unittest.main()
