# UniBot.Market

UniBot 机器人的扩展市场。本仓库维护一个**由机器人自动读取的注册表** `Extensions.json`，
用户通过 **Pull Request** 添加自己的扩展，机器人即可在 WebUI 中搜索并一键安装。

## 目录结构

```
Market/
├── Extensions.json          # 注册表（机器人读取，由 workflow 自动生成，勿手改）
├── extensions/
│   └── <扩展id>.json        # 扩展元数据（用户提交，不包含 sha256）
├── scripts/
│   └── build_registry.py    # 注册表构建脚本（计算 SHA-256）
└── .github/workflows/
    ├── build.yml            # PR 校验 + push 时构建
    └── update.yml           # 定时自动检测新版本
```

## 如何添加你的扩展

1. **准备扩展仓库**：扩展以源码 zip 形式发布在 GitHub Release 上（参考
   [`Extensions/Example`](https://github.com/MineJPGcraft/Minecraft_UniBot/blob/main/Extensions/Example/.github/workflows/release.yml)
   的打包 workflow，zip 根目录需包含 `Extension.toml`）。
2. **新建元数据文件**：在 `extensions/` 目录下创建 `<扩展id>.json`，例如 `Example.json`：

```json
{
  "id": "Example",
  "name": "示例扩展",
  "repo": "MineJPGcraft/Example",
  "description": "一个演示 UniBot 扩展开发流程的示例扩展。"
}
```

   字段说明：

   | 字段 | 必填 | 说明 |
   |------|------|------|
   | `id` | 是 | 扩展唯一标识，须为字母数字下划线，必须与扩展包内 `Extension.toml` 的 `id` 一致 |
   | `name` | 是 | 显示名称 |
   | `repo` | 是 | 扩展源码仓库，格式 `owner/repo` |
   | `description` | 否 | 扩展描述 |

3. **提交 PR**：`build.yml` 会在 PR 上自动校验元数据格式，并到你的扩展仓库拉取
   Release 验证可构建。合并后 `Extensions.json` 会自动生成。

> **为什么不需要填写 sha256？** SHA-256 校验和由本仓库的 workflow 在构建时实时计算，
> 用于防止下载被篡改。用户提交的元数据中不包含也不接受 sha256 字段，保证安全。

## 自动更新

- **PR/push 时**：`build.yml` 重新构建注册表，并为每个扩展拉取最新的 Release 资产，
  重新计算 SHA-256。
- **每天 9 点 / 15 点 / 21 点（北京时间）**：`update.yml` 自动检测各扩展仓库的新
  Release，更新版本号与 SHA-256 并提交回 `main`。

## 本地构建（可选）

无需 GitHub Token 也可对已有 Release 构建（公用 API 有速率限制），需要 Python 3.11+：

```bash
python3 scripts/build_registry.py
```

严格校验模式（等价于 PR 检查）：

```bash
python3 scripts/build_registry.py --validate
```
