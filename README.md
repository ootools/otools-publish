# otools-publish

OTools 宿主与插件的发布仓库，只存放 GitHub Actions 工作流与发布辅助脚本，不存放业务源码。

## 工作流

| 工作流 | 说明 |
| --- | --- |
| `build-otools.yml` | 构建并发布 OTools 宿主安装包与更新清单 |
| `build-otools-plugin.yml` | 构建并发布单个插件（`.oplg`），并同步到插件市场 |
| `build-otools-plugin-standalone.yml` | 构建并发布单个插件的**独立桌面 APP**（安装包 + 专属更新清单） |

> `.oplg` 插件包与独立 APP 是**两条独立的发布线**，tag 与更新端点都分开，互不影响。

## 插件独立 APP 发布流程（`build-otools-plugin-standalone.yml`）

把某个插件打成可独立安装运行的桌面应用（与宿主同源，但只带该插件的功能）。

手动触发，选择插件目录后依次执行：

1. **discover**：读 `plugin.json` 与 `app/tauri.conf.json`，解析 packid / 版本 / identifier，
   并校验插件目录里已有 `app/` 壳；
2. **build-app**：4 平台矩阵（Windows / Linux / macOS arm64 / macOS x64）
   构建前端 → 校验壳未漂移 → 构建 APP → 上传安装包 → 生成该平台的更新清单片段；
3. **publish-manifest**：合并各平台片段成 `latest.json`，写入该插件的**固定 tag**。

### tag 与更新端点约定

| 用途 | tag | 内容 |
| --- | --- | --- |
| 更新端点（**稳定**） | `plugin-app-<packid>` | 只有 `latest.json`，每次发布覆盖 |
| 安装包（保留历史） | `plugin-app-<packid>-v<version>` | `.dmg` / `.app.tar.gz` / `-setup.exe` / `.AppImage` … + `.sig` |

```
更新端点：https://github.com/ootools/otools-publish/releases/download/plugin-app-<packid>/latest.json
```

**为什么不用宿主的 `/releases/latest/`**：GitHub 的 Latest = 「最新的非草稿、非预发布 release」。
插件发得比宿主晚时 Latest 会落到插件 release 上，而它没有 `latest.json` 资产，
于是**所有宿主客户端**的「检查更新」都会报
`Could not fetch a valid release JSON from the remote`。

固定 tag 承载 `latest.json` 是同一问题的对称解法。因此：

- 所有插件 APP 的发布步骤**必须** `make_latest: false`（`tests/test_workflows.py` 会守住这条）；
- 宿主发布（`build-otools.yml`）恰恰**不能**禁用 `make_latest`，否则它自己的端点就失效了。

### 前端与签名

- **必须先构建前端**：`tauri.conf.json` 的 `frontendDist` 指向 `plugins/<x>/dist`，
  它不存在会在 `generate_context!` 阶段**编译期**报错。工作流里由
  `scripts/build-plugin-app-tauri.mjs` 统一处理（含 macOS 的简化 DMG 打包）。
- 更新签名需要 `TAURI_SIGNING_PRIVATE_KEY` / `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`；
  缺少时构建仍会通过，但产不出 `.sig`，脚本会直接失败 —— 没有签名就无法自动更新。
- macOS 上绕过 Tauri 内置 DMG 打包（它走 Finder AppleScript，CI 里不可靠），
  改为「只打 `.app` + `hdiutil` 造简化 DMG」，与宿主 `scripts/run-tauri.js` 同一套思路。

### 前置：插件目录要有 `app/` 壳

壳是生成物，由 OTools 仓库的脚本产出并提交：

```bash
# 在 OTools 仓库里
pnpm build:plugin-app -- plugins/otools-git      # 生成/更新
pnpm check:plugin-app                            # 校验是否漂移
```

目前 OTools 仓库里已有 23 个插件铺开了壳（`otools-aimp`、`otools-audio`、`otools-cftunnel`、
`otools-container`、`otools-dbm`、`otools-disk`、`otools-ftp`、`otools-git`、`otools-http`、
`otools-mqtt`、`otools-nav`、`otools-ngork`、`otools-ollama`、`otools-pakcap`、`otools-portkill`、
`otools-scrcpy`、`otools-servrun`、`otools-sslgo`、`otools-starfish`、`otools-term`、`otools-tts`、
`otools-vm`、`remotecrl`）；其余插件选到会在 **discover** 阶段明确失败并提示修复命令。

## 插件发布流程（`build-otools-plugin.yml`）

手动触发，选择插件目录后依次执行：

1. **discover**：读取 `plugin.json` 元信息，校验插件根目录存在 `logo.svg`；
2. **build-web / build-native**：按插件能力构建前端产物与原生库；
3. **package**：打包 `<packid>-<version>.oplg` 并上传为 Release 产物，
   tag 形如 `plugin-<packid>-v<version>`；
4. **sync-market**：把本次发布同步到插件市场。

### sync-market 做什么

按「先让 logo 可访问、再写入插件市场」的顺序执行：

1. 校验 Release 中的 `.oplg` 地址可下载（HTTP 200）；
2. 把插件根目录的 `logo.svg` 写入 `ootools/otools-website` 仓库的
   `public/plugin-logos/<packid>.svg` 并提交推送，由该仓库的 GitHub Pages 站点托管，
   线上地址为 `https://otools.lingyun.net/plugin-logos/<packid>.svg`；
3. 调用 xycloud 的插件发布接口，插件不存在则创建，存在则更新插件元信息、
   `.oplg` 包地址（`packageUrl`）与 logo 线上地址（`logo`）：

   ```
   POST https://otools-api.lingyun.net/api/v1/otools/plugin/publish
   Header: X-OTools-Token: <OTOOLS_MARKET_TOKEN>
   ```

### 本地预演

同步脚本可以脱离 CI 单独运行，`--dry-run` 只打印将提交的内容：

```bash
python scripts/sync-plugin-market.py \
  --plugin-dir /path/to/OTools/plugins/otools-git \
  --website-dir /path/to/otools-website \
  --release-tag plugin-otools-git-v0.1.0 \
  --dry-run
```

常用开关：

| 参数 | 说明 |
| --- | --- |
| `--skip-logo` | 只校验 logo，不写入官网仓库（CI 提交元信息阶段使用） |
| `--skip-submit` | 只写入 logo，不调用插件市场接口（CI 拷贝 logo 阶段使用） |
| `--no-official` | 标记为非官方插件，不加入 `official` 分类 |

### 测试

```bash
python tests/test_sync_plugin_market.py
python tests/test_build_plugin_changelog.py
python tests/test_build_plugin_app_latest_json.py
python tests/test_workflows.py
```

覆盖包地址拼装、logo 拷贝与变更检测、提交内容契约、`GITHUB_OUTPUT` 写出，
`--dry-run` / `--skip-submit` / 缺令牌 等分支；独立 APP 更新清单的
包型识别、平台/架构映射、URL 编码（`productName` 含空格）、片段合并；
以及工作流的发布安全不变量（插件发布必须 `make_latest: false`，宿主发布必须不禁用）。

## 配置

### Secrets

| 名称 | 说明 |
| --- | --- |
| `OTOOLS_REPO_TOKEN` | 克隆 OTools 及其子模块；若该令牌对 `ootools/otools-website` 也有写权限，可同时用于同步 logo |
| `OTOOLS_MARKET_TOKEN` | 插件市场发布令牌，需与后端 `.env` 的 `[OTOOLS] publish_token` 一致 |
| `OTOOLS_WEBSITE_TOKEN` | 可选。对 `ootools/otools-website` 有写权限的 PAT。**不配置时自动回退到 `OTOOLS_REPO_TOKEN`** |

> 为什么需要 PAT 而不是默认的 `GITHUB_TOKEN`？
> GitHub 规定：用仓库自带的 `GITHUB_TOKEN` 发起的 push **不会**再触发其他工作流
> （防止递归触发）。所以若用 `GITHUB_TOKEN` 推送 logo，`otools-website` 的
> Pages 部署工作流不会被触发，logo 提交了也不会部署上线。
> 只有当 `OTOOLS_REPO_TOKEN` 对 `ootools/otools-website` 没有写权限时，才需要单独配置
> `OTOOLS_WEBSITE_TOKEN`。

### Variables（可选）

| 名称 | 默认值 |
| --- | --- |
| `OTOOLS_MARKET_API` | `https://otools-api.lingyun.net/api/v1/otools/plugin/publish` |
| `OTOOLS_WEBSITE_BASE_URL` | `https://otools.lingyun.net` |

### 后端配合

插件市场后端（`xycloud` 的 `otools` 应用）需要：

- 执行迁移 `app/_app/otools/install/2026-09-16-otools-plugin-logo.sql`
  （新增 `logo` 字段，并补齐发布同步所需字段）；
- 在 `.env` 中配置发布令牌：

  ```ini
  [OTOOLS]
  publish_token = <与 OTOOLS_MARKET_TOKEN 相同的随机串>
  ```

- 部署后清理一次路由注解缓存，使 `POST /api/v1/otools/plugin/publish` 生效。

## 插件根目录要求

- 必须存在 `plugin.json`；
- **必须存在 `logo.svg`**，否则发布在 discover 阶段直接失败。
