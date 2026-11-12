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


class HostLinuxDiagnosticsWorkflowTests(unittest.TestCase):
    def workflow(self):
        return (WORKFLOW_DIR / "build-otools.yml").read_text(encoding="utf-8")

    def test_linux_only_run_cannot_publish_an_incomplete_updater_manifest(self):
        text = self.workflow()
        job = text.split("\n  publish-updater-manifest:", 1)[1]
        header = job.split("\n    steps:", 1)[0]
        self.assertIn("github.event.inputs.build_platform != 'linux'", header)
        self.assertIn("build_platform:", text)
        self.assertIn("fromJSON(github.event.inputs.build_platform == 'linux'", text)

    def test_linux_build_runs_diagnostics_before_uploading_its_artifacts(self):
        text = self.workflow()
        steps = split_steps(text)
        build = next(step for step in steps if "name: Build the App (Linux/MacOS)" in step)
        self.assertIn("ci-build-diagnostics.py", build)
        self.assertIn("pnpm tauri:build", build)
        # Linux 换页卡死时必须以「步骤失败」收尾：整跑被取消不会执行 post 步骤，
        # diagnostics artifact 与 rust-cache 都可能丢失。
        self.assertIn("timeout -k 30 60m pnpm tauri:build", build)
        upload = next((step for step in steps if "name: Upload Linux build diagnostics" in step), "")
        self.assertIn("always()", upload)
        self.assertIn("matrix.platform == 'ubuntu-24.04'", upload)
        self.assertIn("otools-build-diagnostics", upload)

    def test_failed_build_still_warms_the_rust_cache(self):
        """Linux release 全量编译 30 分钟以上；失败不存缓存会陷入每次从头重编的死循环。"""
        steps = split_steps(self.workflow())
        cache = next(step for step in steps if "Swatinem/rust-cache@v2" in step)
        self.assertIn("cache-on-failure: true", cache)

    def test_rust_cache_points_at_the_real_target_dir(self):
        """`-> target` 相对 workspace 根解析；写错会静默只缓存 cargo registry。

        0.9.0 实测：`OTools/src-tauri -> OTools/target` 指向不存在的
        `OTools/src-tauri/OTools/target`，缓存只有 ~280 MB，编译产物从未入库，
        导致每次 Linux 发布都是 30 分钟以上的全量冷编译。
        """
        steps = split_steps(self.workflow())
        cache = next(step for step in steps if "Swatinem/rust-cache@v2" in step)
        self.assertIn("\n            OTools/src-tauri -> ../target\n", cache)
        self.assertNotIn("\n            OTools/src-tauri -> OTools/target\n", cache)
        self.assertIn("cache-targets: ${{ startsWith(matrix.platform, 'ubuntu') }}", cache)

    def test_linux_build_has_extra_swap_before_lto_link(self):
        """fat-LTO 链接峰值 15-16 GiB 会触发 oom_kill；必须先补足 swap。"""
        steps = split_steps(self.workflow())
        swap_idx = next(
            i for i, step in enumerate(steps) if "name: Add Linux build swap" in step
        )
        build_idx = next(
            i for i, step in enumerate(steps) if "name: Build the App (Linux/MacOS)" in step
        )
        self.assertLess(swap_idx, build_idx)
        swap = steps[swap_idx]
        self.assertIn("matrix.platform == 'ubuntu-24.04'", swap)
        self.assertIn("swapon /mnt/swapfile-extra", swap)

    def test_linux_disables_lto_without_changing_other_platforms(self):
        """Linux 用 Cargo 官方环境变量降内存，macOS/Windows 必须保持原配置。

        实测（run 43196929061，cgu=1 复跑）：主 crate `src-tauri` 的单个 rustc
        仍有两次 ~14.7 GiB 匿名页峰值，随后 swap 14.4 GiB、pgscan 7.5e7 换页死锁。
        codegen-units 16/1 峰值相同，说明不是 CGU 拆分问题；本地 `cargo check`
        （expansion + typeck）不足 1 GiB，说明 14.7 GiB 全在 MIR 优化 + LLVM codegen。
        故 Linux 分支额外把 opt-level 降到 2，配合 LTO=off / debug=0 / 单 rustc 作业。
        Cargo 环境变量优先级高于 Cargo.toml，仓库配置一字不动；Linux 产物本就
        strip="debuginfo"，关 debug 不改变最终二进制。
        """
        steps = split_steps(self.workflow())
        build = next(step for step in steps if "name: Build the App (Linux/MacOS)" in step)
        for var in ("LTO", "DEBUG", "CODEGEN_UNITS", "OPT_LEVEL"):
            self.assertNotIn(
                f"CARGO_PROFILE_RELEASE_{var}:",
                build,
                "不能写进 step 级 env，否则 macOS/Windows 也会被降级",
            )
        cut = build.index("\n          else\n")
        linux_branch, other_platforms = build[:cut], build[cut:]
        for setting in (
            "export CARGO_PROFILE_RELEASE_OPT_LEVEL=2",
            "export CARGO_PROFILE_RELEASE_LTO=off",
            "export CARGO_PROFILE_RELEASE_DEBUG=0",
            "export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1",
            "export CARGO_BUILD_JOBS=1",
        ):
            self.assertIn(setting, linux_branch)
            self.assertLess(
                linux_branch.index(setting),
                linux_branch.index("ci-build-diagnostics.py"),
                "env 必须在启动构建（诊断脚本）之前 export",
            )
        self.assertNotIn("CARGO_PROFILE_RELEASE", other_platforms)
        self.assertIn("pnpm tauri:build", other_platforms)


class PluginRustCacheTargetDirTests(unittest.TestCase):
    """插件线 rust-cache 的 target 必须相对 workspace 根，不能写全路径。

    rust-cache 源码里是 `path.join(root, target)`：写
    `OTools/<adapter>/native -> OTools/<adapter>/native/target` 会解析成
    `OTools/<adapter>/native/OTools/<adapter>/native/target`（不存在），
    缓存静默退化成只有 cargo registry，插件每次发布都全量重编。
    """

    def test_plugin_workflows_use_relative_target_dir(self):
        for name in ("build-otools-plugin.yml", "build-otools-plugin-standalone.yml"):
            path = WORKFLOW_DIR / name
            steps = split_steps(path.read_text(encoding="utf-8"))
            cache = next(step for step in steps if "Swatinem/rust-cache@v2" in step)
            mapping = [
                line.strip()
                for line in cache.splitlines()
                if "->" in line and not line.lstrip().startswith("#")
            ]
            self.assertEqual(len(mapping), 1, f"{name} 的 workspaces 映射不唯一")
            self.assertTrue(
                mapping[0].endswith("-> target"),
                f"{name} 的 target 写成全路径会被拼成双重前缀:\n{mapping[0]}",
            )

    def test_plugin_native_build_really_puts_target_inside_workspace(self):
        """`-> target` 成立的前提：cargo 确实把产物放在 workspace 内的 target/。"""
        text = (WORKFLOW_DIR / "build-otools-plugin.yml").read_text(encoding="utf-8")
        self.assertIn(
            '--target-dir "OTools/${{ needs.discover.outputs.adapter_root }}/native/target"',
            text,
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
