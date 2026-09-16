# otools-publish

OTools 宿主与插件的发布仓库，只存放 GitHub Actions 工作流与发布辅助脚本，不存放业务源码。

## 工作流

| 工作流 | 说明 |
| --- | --- |
| `build-otools.yml` | 构建并发布 OTools 宿主安装包与更新清单 |
| `build-otools-plugin.yml` | 构建并发布单个插件（`.oplg`），并同步到插件市场 |

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
```

覆盖包地址拼装、logo 拷贝与变更检测、提交内容契约、`GITHUB_OUTPUT` 写出，以及
`--dry-run` / `--skip-submit` / 缺令牌 等分支。

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
