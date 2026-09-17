"""sync-plugin-market.py 的单元测试。

脚本文件名带连字符，无法直接 import，这里用 importlib 按路径加载。
运行：python tests/test_sync_plugin_market.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-plugin-market.py"

spec = importlib.util.spec_from_file_location("sync_plugin_market", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_plugin(*, with_logo: bool = True, nested: bool = False, packid: str = "otools-git") -> Path:
    plugin_dir = Path(tempfile.mkdtemp())
    adapter_root = plugin_dir / "otools" if nested else plugin_dir
    write_file(
        adapter_root / "plugin.json",
        json.dumps(
            {
                "packid": packid,
                "uuid": packid,
                "displayName": "Git",
                "displayNameCN": "章鱼Git",
                "developerName": "OTools",
                "summary": "Git 仓库管理工具。",
                "version": "0.1.0",
                "icon": "@builtin:git",
                "entry": "dist/index.html",
                "screenshots": [],
            },
            ensure_ascii=False,
        ),
    )
    if with_logo:
        write_file(adapter_root / "logo.svg", "<svg />")
    return plugin_dir


class SyncPluginMarketTests(unittest.TestCase):
    def test_build_package_url(self) -> None:
        url = module.build_package_url(
            "ootools/otools-publish", "plugin-otools-git-v0.1.0", "otools-git", "0.1.0"
        )
        self.assertEqual(
            url,
            "https://github.com/ootools/otools-publish/releases/download/"
            "plugin-otools-git-v0.1.0/otools-git-0.1.0.oplg",
        )

    def test_resolve_adapter_root_flat_and_nested(self) -> None:
        flat = make_plugin()
        self.assertEqual(module.resolve_adapter_root(flat), flat)

        nested = make_plugin(nested=True)
        self.assertEqual(module.resolve_adapter_root(nested), nested / "otools")

    def test_resolve_adapter_root_without_manifest_fails(self) -> None:
        empty = Path(tempfile.mkdtemp())
        with self.assertRaises(SystemExit):
            module.resolve_adapter_root(empty)

    def test_resolve_logo_requires_logo_svg(self) -> None:
        plugin_dir = make_plugin(with_logo=True)
        self.assertEqual(module.resolve_logo(plugin_dir, plugin_dir), plugin_dir / "logo.svg")

        missing = make_plugin(with_logo=False)
        with self.assertRaises(SystemExit):
            module.resolve_logo(missing, missing)

    def test_copy_logo_reports_change_state(self) -> None:
        plugin_dir = make_plugin()
        website_dir = Path(tempfile.mkdtemp())

        target, changed = module.copy_logo(plugin_dir / "logo.svg", website_dir, "otools-git")
        self.assertTrue(changed)
        self.assertEqual(target, website_dir / "public" / "plugin-logos" / "otools-git.svg")
        self.assertEqual(target.read_text(encoding="utf-8"), "<svg />")

        # 内容一致时不应重复写入
        _, changed_again = module.copy_logo(plugin_dir / "logo.svg", website_dir, "otools-git")
        self.assertFalse(changed_again)

        # 内容变化时应重新写入
        write_file(plugin_dir / "logo.svg", "<svg>v2</svg>")
        _, changed_after_update = module.copy_logo(plugin_dir / "logo.svg", website_dir, "otools-git")
        self.assertTrue(changed_after_update)
        self.assertEqual(target.read_text(encoding="utf-8"), "<svg>v2</svg>")

    def test_build_payload_contract(self) -> None:
        manifest = json.loads((make_plugin() / "plugin.json").read_text(encoding="utf-8"))
        payload = module.build_payload(
            manifest,
            "otools-git",
            "0.1.0",
            "https://otools.lingyun.net/plugin-logos/otools-git.svg",
            "https://github.com/ootools/otools-publish/releases/download/t/a.oplg",
            True,
        )
        self.assertEqual(payload["packid"], "otools-git")
        self.assertEqual(payload["uuid"], "otools-git")
        self.assertEqual(payload["displayNameCN"], "章鱼Git")
        self.assertEqual(payload["logo"], "https://otools.lingyun.net/plugin-logos/otools-git.svg")
        self.assertTrue(payload["packageUrl"].endswith("a.oplg"))
        self.assertTrue(payload["official"])
        self.assertIs(payload["supportMacos"], True)

    def test_build_payload_omits_curated_fields(self) -> None:
        """截图、最低宿主版本等由后台人工维护，plugin.json 未声明时不能提交空值覆盖。"""
        manifest = json.loads((make_plugin() / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("screenshots"), [])
        self.assertNotIn("minOToolsVersion", manifest)

        payload = module.build_payload(manifest, "otools-git", "0.1.0", "logo", "pkg", True)
        self.assertNotIn("screenshots", payload, "空截图不应提交，否则会清空后台已有截图")
        self.assertNotIn("minOToolsVersion", payload, "未声明的最低版本不应提交")
        # categories 含 hot / featured 等运营标签，交由后端按 official 合并，不能整体覆盖
        self.assertNotIn("categories", payload)

    def test_build_payload_keeps_declared_screenshots(self) -> None:
        manifest = json.loads((make_plugin() / "plugin.json").read_text(encoding="utf-8"))
        manifest["screenshots"] = ["https://a.png"]
        payload = module.build_payload(manifest, "otools-git", "0.1.0", "logo", "pkg", True)
        self.assertEqual(payload["screenshots"], ["https://a.png"])

    def test_build_payload_includes_changelog(self) -> None:
        """更新说明由流水线按插件目录生成，多行文本要原样带过去。"""
        manifest = json.loads((make_plugin() / "plugin.json").read_text(encoding="utf-8"))
        payload = module.build_payload(
            manifest, "otools-git", "0.1.0", "logo", "pkg", True, "- 修好树展开\n- 新增导出"
        )
        self.assertEqual(payload["changelog"], "- 修好树展开\n- 新增导出")

    def test_build_payload_omits_empty_changelog(self) -> None:
        """没生成说明时不提交该字段，避免用空串覆盖后台人工维护的内容。"""
        manifest = json.loads((make_plugin() / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn(
            "changelog",
            module.build_payload(manifest, "otools-git", "0.1.0", "logo", "pkg", True),
        )
        self.assertNotIn(
            "changelog",
            module.build_payload(manifest, "otools-git", "0.1.0", "logo", "pkg", True, "   "),
        )

    def test_build_payload_falls_back_to_packid(self) -> None:
        payload = module.build_payload({}, "otools-x", "1.0.0", "logo", "pkg", True)
        self.assertEqual(payload["uuid"], "otools-x")
        self.assertEqual(payload["displayName"], "otools-x")
        self.assertNotIn("summary", payload)

    def test_write_github_output(self) -> None:
        output = Path(tempfile.mkdtemp()) / "github_output"
        previous = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = str(output)
        try:
            module.write_github_output({"packid": "otools-git", "logo_changed": "true"})
            module.write_github_output({"market_action": "updated"})
        finally:
            if previous is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = previous

        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines, ["packid=otools-git", "logo_changed=true", "market_action=updated"]
        )

    def test_write_github_output_without_env_is_noop(self) -> None:
        previous = os.environ.pop("GITHUB_OUTPUT", None)
        try:
            module.write_github_output({"packid": "otools-git"})
        finally:
            if previous is not None:
                os.environ["GITHUB_OUTPUT"] = previous

    def test_main_dry_run_does_not_touch_website(self) -> None:
        plugin_dir = make_plugin()
        website_dir = Path(tempfile.mkdtemp())
        argv = sys.argv
        sys.argv = [
            "sync-plugin-market.py",
            "--plugin-dir",
            str(plugin_dir),
            "--website-dir",
            str(website_dir),
            "--release-tag",
            "plugin-otools-git-v0.1.0",
            "--dry-run",
        ]
        try:
            stream = io.StringIO()
            with redirect_stdout(stream):
                module.main()
        finally:
            sys.argv = argv

        output = stream.getvalue()
        self.assertIn("dry-run", output)
        self.assertIn("otools-git-0.1.0.oplg", output)
        self.assertFalse((website_dir / "public").exists(), "dry-run 不应写入官网仓库")

    def test_main_skip_submit_writes_logo_only(self) -> None:
        plugin_dir = make_plugin()
        website_dir = Path(tempfile.mkdtemp())
        output_file = Path(tempfile.mkdtemp()) / "github_output"
        previous = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = str(output_file)
        argv = sys.argv
        sys.argv = [
            "sync-plugin-market.py",
            "--plugin-dir",
            str(plugin_dir),
            "--website-dir",
            str(website_dir),
            "--skip-submit",
        ]
        try:
            with redirect_stdout(io.StringIO()):
                module.main()
        finally:
            sys.argv = argv
            if previous is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = previous

        self.assertTrue((website_dir / "public" / "plugin-logos" / "otools-git.svg").is_file())
        values = dict(
            line.split("=", 1)
            for line in output_file.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        self.assertEqual(values["packid"], "otools-git")
        self.assertEqual(values["logo_changed"], "true")
        self.assertEqual(
            values["logo_url"], "https://otools.lingyun.net/plugin-logos/otools-git.svg"
        )

    def test_main_requires_token_when_submitting(self) -> None:
        plugin_dir = make_plugin()
        website_dir = Path(tempfile.mkdtemp())
        argv = sys.argv
        sys.argv = [
            "sync-plugin-market.py",
            "--plugin-dir",
            str(plugin_dir),
            "--website-dir",
            str(website_dir),
            "--skip-logo",
        ]
        try:
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    module.main()
        finally:
            sys.argv = argv


if __name__ == "__main__":
    unittest.main()
