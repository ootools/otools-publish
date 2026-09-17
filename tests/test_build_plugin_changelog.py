"""build-plugin-changelog.py 的单元测试。

脚本文件名带连字符，无法直接 import，这里用 importlib 按路径加载。
运行：python tests/test_build_plugin_changelog.py
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-plugin-changelog.py"

spec = importlib.util.spec_from_file_location("build_plugin_changelog", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PluginSourcePathTests(unittest.TestCase):
    def test_plain_dir(self) -> None:
        self.assertEqual(module.plugin_source_path("otools-dbm"), "plugins/otools-dbm")

    def test_strips_slashes_and_spaces(self) -> None:
        self.assertEqual(module.plugin_source_path("  /otools-git/  "), "plugins/otools-git")

    def test_custom_prefix(self) -> None:
        self.assertEqual(module.plugin_source_path("otools-git", "apps"), "apps/otools-git")

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(module.plugin_source_path("   "), "")


class PickPreviousPublishedAtTests(unittest.TestCase):
    def test_picks_latest_other_version(self) -> None:
        releases = [
            {"tag_name": "plugin-otools-dbm-v0.1.1", "published_at": "2026-09-16T17:31:49Z"},
            {"tag_name": "plugin-otools-dbm-v0.1.0", "published_at": "2026-09-10T08:00:00Z"},
        ]
        self.assertEqual(
            module.pick_previous_published_at(releases, "otools-dbm", "0.1.2"),
            "2026-09-16T17:31:49Z",
        )

    def test_excludes_version_being_published(self) -> None:
        releases = [
            {"tag_name": "plugin-otools-dbm-v0.1.2", "published_at": "2026-09-17T08:21:58Z"},
            {"tag_name": "plugin-otools-dbm-v0.1.1", "published_at": "2026-09-16T17:31:49Z"},
        ]
        self.assertEqual(
            module.pick_previous_published_at(releases, "otools-dbm", "0.1.2"),
            "2026-09-16T17:31:49Z",
        )

    def test_ignores_other_plugins_and_host_tags(self) -> None:
        releases = [
            {"tag_name": "plugin-otools-git-v0.1.2", "published_at": "2026-09-17T08:07:18Z"},
            {"tag_name": "v0.7.7", "published_at": "2026-09-17T09:20:45Z"},
        ]
        self.assertEqual(module.pick_previous_published_at(releases, "otools-dbm", "0.1.2"), "")

    def test_empty_release_list(self) -> None:
        self.assertEqual(module.pick_previous_published_at([], "otools-dbm", "0.1.2"), "")


class CommitSubjectsTests(unittest.TestCase):
    def test_first_line_only_and_dedupe(self) -> None:
        commits = [
            {"commit": {"message": "fix(dbm): 修好树展开\n\n正文说明"}},
            {"commit": {"message": "feat(dbm): 新增导出"}},
            {"commit": {"message": "fix(dbm): 修好树展开"}},
            {"commit": {"message": "   "}},
            {"commit": {}},
            {"unexpected": "shape"},
        ]
        self.assertEqual(
            module.commit_subjects(commits),
            ["fix(dbm): 修好树展开", "feat(dbm): 新增导出"],
        )

    def test_empty(self) -> None:
        self.assertEqual(module.commit_subjects([]), [])
        self.assertEqual(module.commit_subjects(None), [])


class RenderChangelogTests(unittest.TestCase):
    def test_markdown_list(self) -> None:
        self.assertEqual(
            module.render_changelog(["a", "b"]),
            "- a\n- b",
        )

    def test_respects_max_commits(self) -> None:
        self.assertEqual(module.render_changelog(["a", "b", "c"], 2), "- a\n- b")

    def test_empty_renders_empty_string(self) -> None:
        self.assertEqual(module.render_changelog([]), "")

    def test_max_commits_floor(self) -> None:
        # 上限传 0 / 负数时至少保留 1 条，避免生成完全空的说明
        self.assertEqual(module.render_changelog(["a", "b"], 0), "- a")


class BuildCommitsUrlTests(unittest.TestCase):
    def test_with_since(self) -> None:
        url = module.build_commits_url(
            "https://api.github.com", "ootools/OTools", "plugins/otools-dbm", "2026-09-16T17:31:49Z"
        )
        self.assertIn("/repos/ootools/OTools/commits?", url)
        self.assertIn("path=plugins%2Fotools-dbm", url)
        self.assertIn("since=2026-09-16T17%3A31%3A49Z", url)

    def test_without_since(self) -> None:
        url = module.build_commits_url(
            "https://api.github.com", "ootools/OTools", "plugins/otools-dbm", ""
        )
        self.assertNotIn("since=", url)


class GithubOutputTests(unittest.TestCase):
    def test_multiline_uses_heredoc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "github_output"
            previous = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output)
            try:
                module.write_github_output({"changelog": "- a\n- b", "commit_count": "2"})
            finally:
                if previous is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = previous
            content = output.read_text(encoding="utf-8")
            self.assertIn("changelog<<CHANGELOG_EOF", content)
            self.assertIn("- a\n- b", content)
            self.assertIn("CHANGELOG_EOF", content)
            self.assertIn("commit_count=2", content)

    def test_no_github_output_is_noop(self) -> None:
        previous = os.environ.get("GITHUB_OUTPUT")
        os.environ.pop("GITHUB_OUTPUT", None)
        try:
            module.write_github_output({"changelog": "- a"})
        finally:
            if previous is not None:
                os.environ["GITHUB_OUTPUT"] = previous


if __name__ == "__main__":
    unittest.main()
