"""工作流的发布安全不变量测试。

只用标准库（不依赖 PyYAML）：按行切出「步骤块」再做结构判断。
运行：python tests/test_workflows.py

守的是一条踩过坑的规则：

- **插件发布**（`.oplg` 与独立 APP）必须显式 `make_latest: false`。
  GitHub 的 Latest = 「最新的非草稿、非预发布 release」，插件发得比宿主晚时
  Latest 会落到插件 release 上，而它没有 `latest.json` 资产，
  于是所有宿主客户端的「检查更新」都报
  `Could not fetch a valid release JSON from the remote`。
- **宿主发布**（`build-otools.yml`）恰恰相反：它必须成为 Latest，
  否则 `/releases/latest/download/latest.json` 这个更新端点会失效。
"""

import re
import unittest
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RELEASE_ACTION = "softprops/action-gh-release"

# 一个步骤的起始行：缩进后的 `- name:` / `- uses:` / `- run:` …
STEP_START = re.compile(r"^\s*-\s+(name|uses|run|if|id|with|env|shell|working-directory):")


def split_steps(text: str):
    """把工作流文本切成步骤块（粗粒度：只按步骤起始行切，不解析 YAML）。"""
    steps = []
    current = []
    for line in text.splitlines():
        if STEP_START.match(line) and current:
            steps.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        steps.append("\n".join(current))
    return steps


def release_steps(path: Path):
    """取出该工作流里所有「发布 release」的步骤块。"""
    text = path.read_text(encoding="utf-8")
    return [step for step in split_steps(text) if RELEASE_ACTION in step]


class PluginWorkflowMakeLatestTests(unittest.TestCase):
    """插件发布线：所有 release 上传都必须 make_latest: false。"""

    def plugin_workflows(self):
        files = sorted(WORKFLOW_DIR.glob("build-otools-plugin*.yml"))
        self.assertTrue(files, "未找到插件发布工作流")
        return files

    def test_every_release_step_disables_make_latest(self) -> None:
        for path in self.plugin_workflows():
            steps = release_steps(path)
            self.assertTrue(steps, f"{path.name} 没有 release 上传步骤")
            for step in steps:
                self.assertIn(
                    "make_latest: false",
                    step,
                    f"{path.name} 有 release 步骤未禁用 make_latest（会抢走宿主的 Latest）:\n{step}",
                )

    def test_standalone_workflow_uses_per_plugin_endpoint_tags(self) -> None:
        """独立 APP 必须用「固定 tag 承载清单 + 版本化 tag 承载安装包」两条 tag。"""
        path = WORKFLOW_DIR / "build-otools-plugin-standalone.yml"
        self.assertTrue(path.is_file(), "缺少独立 APP 发布工作流")
        text = path.read_text(encoding="utf-8")

        # 固定 tag（更新端点）
        self.assertIn("release_tag", text)
        self.assertIn("plugin-app-${packid}", text)
        # 版本化 tag（安装包）
        self.assertIn("artifact_tag", text)
        self.assertIn("plugin-app-${packid}-v${version}", text)
        # 绝不能回落到宿主那种 /releases/latest/ 端点 —— 整份工作流里都不允许出现
        self.assertNotIn(
            "releases/latest/download",
            text,
            "独立 APP 不能使用宿主的 /releases/latest/ 端点（会拿到插件自己的 release，它没有 latest.json）",
        )

    def test_standalone_workflow_builds_the_app_shell_check(self) -> None:
        """发布前必须校验壳没漂移，否则命令清单变了也不会有感知。"""
        path = WORKFLOW_DIR / "build-otools-plugin-standalone.yml"
        text = path.read_text(encoding="utf-8")
        self.assertIn("build-plugin-app.mjs", text)
        self.assertIn("--check", text)
        self.assertIn("build-plugin-app-tauri.mjs", text)


class HostWorkflowMakeLatestTests(unittest.TestCase):
    """宿主发布线：恰恰不能禁用 make_latest，否则 /releases/latest/ 端点失效。"""

    def test_host_release_steps_do_not_disable_make_latest(self) -> None:
        path = WORKFLOW_DIR / "build-otools.yml"
        self.assertTrue(path.is_file(), "缺少宿主发布工作流")
        steps = release_steps(path)
        self.assertTrue(steps, "宿主工作流没有 release 上传步骤")
        for step in steps:
            self.assertNotIn(
                "make_latest: false",
                step,
                f"{path.name} 的宿主发布禁用了 make_latest，会导致更新端点失效:\n{step}",
            )


class PluginAppLatestJsonScriptTests(unittest.TestCase):
    """清单脚本必须与工作流里写死的 tag 约定一致。"""

    def test_script_uses_artifact_tag_for_asset_urls(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "build-plugin-app-latest-json.py"
        text = script.read_text(encoding="utf-8")
        # 资产 URL 必须指向 artifact_tag（版本化 tag），而不是固定 tag
        self.assertIn("releases/download/{artifact_tag}/", text)


if __name__ == "__main__":
    unittest.main()
