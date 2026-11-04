# Claude 内置浏览器 Bypass 补丁

[English](README.md)

让 Claude Desktop 在**第三方模型／网关模式**下，也根据当前会话的 **Bypass permissions** 跳过内置浏览器的普通站点审批。

这是社区实验工具，与 Anthropic 无关联。**目前仅在 Apple silicon Mac 的 Claude Desktop 4.16336.0 上完成运行验证；其他版本会拒绝修改。** Windows、Linux 不支持应用补丁。

## 应用前了解签名影响

修改官方安装包后，需要重新进行本地签名。因此本工具会：

- 先完整备份并验证原版，在副本上打补丁，成功后再替换安装包。
- 保留 ASAR 完整性校验，重算文件、归档头和 Electron 框架内的摘要。
- 使用本地临时签名，并调整该应用主进程和 Electron 辅助进程的库签名校验。
- 移除本地签名无法保留的厂商身份与共享钥匙串权限。**依赖这些身份的企业登录、通行密钥等功能可能受影响，系统隐私权限也可能需要重新授予。**

这些签名调整是运行本地修改版所需的兼容处理。请仅在自己控制、且部署政策允许修改的设备和应用上使用。

工具不会修改模型、网关、密钥、钥匙串内容、插件配置或 Allowed sites 列表，也不会关闭系统 SIP／Gatekeeper、结束 Claude 进程、安装后台程序或关闭自动更新。Python 工具没有联网功能。

## 使用

需要 Python 3.10+、macOS 自带的 `codesign`／`ditto`／`ps`，以及足够容纳备份和副本的磁盘空间。需要对安装目录有写入权限。Intel Mac 尚未完成运行验证。

```sh
git clone https://github.com/TheNight-Watch/claude-browser-bypass-patch.git
cd claude-browser-bypass-patch
python3 claude_browser_bypass.py check
```

`check` 只读取和检查，不修改应用。它会核对版本、已知代码结构、完整性摘要和当前签名。

完全退出 Claude 及其辅助进程后，明确接受本地签名影响，再执行：

```sh
python3 claude_browser_bypass.py apply --accept-local-signing
```

重新打开 Claude，在目标第三方会话中选择 **Bypass permissions**。用尚未加入 Allowed sites 的普通公共网页验证。

如果应用不在默认 `/Applications/Claude.app`，给每个命令指定路径：

```sh
python3 claude_browser_bypass.py check --app ~/Applications/Claude.app
```

工具不会请求管理员提权。安装目录不可写时，请使用自己拥有写入权限的安装副本。

## 恢复原版

`apply` 会输出 `backup` 目录，默认位于：

```text
~/Library/Application Support/claude-browser-bypass-patch/backups/
```

完全退出 Claude 后，使用输出中的确切备份目录：

```sh
python3 claude_browser_bypass.py restore --backup "/apply输出的备份目录"
```

恢复前会验证备份摘要、原版签名、目标路径和版本。只要安装版本信息仍能读取，也可以恢复丢失或损坏的 ASAR。工具不会默默降级其他已安装版本；用户配置和钥匙串内容不在其修改范围内。

## 生效范围

| 场景 | 行为 |
| --- | --- |
| 第三方会话 + Bypass，普通 Browser 站点审批 | 跳过 |
| 第三方 Auto、Manual、Plan、Accept edits | 保留原行为 |
| 官方模式原有 Auto／Bypass 行为 | 保留 |
| 没有会话或权限查询出错 | 继续要求审批 |
| 站点安全、组织规则、私网处理 | 仍由原有检查控制 |
| Web Search、WebFetch | 不修改 |
| 模型网关、密钥、插件配置 | 不修改 |
| 官方更新 | 保留；可能覆盖补丁 |

这不是保证所有网站都能访问的开关。它只调整一个权限判断，其他原因仍可能让网站不可用。

## 测试与更新

底层补丁已在实际第三方 Bypass 会话中验证：内置 Browser 打开了未预先授权的 IANA 和 Python 官网，读取标题时没有出现站点审批。这不代表所有浏览器操作或企业登录功能都已验证。

公开版包含自动测试，覆盖权限行为、完整性重算、未知版本拒绝、重复运行、备份、安装失败回退和损坏应用恢复。开发测试使用合成数据，不分发 Claude 安装包或厂商 JavaScript 文件。公开封装流程另在真实应用的隔离副本上验证。

```sh
python3 -m unittest discover -s tests -v
```

Node.js 只用于开发测试，不是用户运行补丁的依赖。

另外附有 [GitHub Actions 模板](ci/github-actions.example.yml)，默认不启用。仓库所有者可以审查后，用具备工作流写入权限的账号将其复制到 `.github/workflows/test.yml`。当前公布的是 21 项本地测试通过，尚未运行模板中的 GitHub 托管测试。

官方更新后请重新运行 `check`。新版本可能覆盖补丁；未知版本会被拒绝，需重新确认兼容性。请保留自动更新和系统安全保护。

代码采用 [MIT 许可证](LICENSE)，不包含 Claude、Electron 安装包及其许可证授权。更多实现参考和背景见 [英文说明](README.md)。
