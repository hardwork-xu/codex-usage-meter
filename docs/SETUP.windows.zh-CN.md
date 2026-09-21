# Windows 首次安装与启用

新用户推荐 [直接通过 Codex 安装](SETUP.github.zh-CN.md)，无需下载源码运行安装器。下面保留已有 `personal` 安装渠道及手动源码安装流程；两种渠道选择一种即可。

本文适用于 **Windows 原生 Codex + PowerShell + Python 3.10 或更新版本**。安装器使用 Codex 官方插件辅助程序和个人市场，不申请管理员权限，也不调整沙箱、审批或 PowerShell 执行策略。安装成功后，Hooks 仍需要你亲自审阅并信任。

Windows 原生代理与 WSL 是不同运行环境。本文的安装目录、Python 和 Hooks 均属于 Windows；如果代理实际运行在 WSL，请在 WSL 环境单独安装，不混用两边的路径。[官方 Windows 环境说明](https://learn.chatgpt.com/docs/windows/windows-app)

## 1. 准备 Python 与 Codex

1. 安装并登录支持插件的 Codex 桌面界面。确认代理环境为 **Windows native**，使用 **PowerShell**；集成终端选项与代理环境是两个不同设置。
2. 如尚无 Python，从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)安装适合当前电脑的版本。使用常规安装程序时，按需要启用 Python 启动器或将 Python 加入用户的 `PATH`。
3. 从开始菜单打开一个新的 **PowerShell** 窗口，执行：

   ```powershell
   py -3 --version
   ```

   需要显示 Python 3.10 或更高版本。如果没有 `py`，可以验证：

   ```powershell
   python --version
   ```

4. 确认 Codex 命令可用，并已按正常流程登录：

   ```powershell
   codex --version
   ```

   如果找不到命令，先参照 [Codex CLI 官方安装说明](https://learn.chatgpt.com/docs/cli)完成安装，再新开 PowerShell。安装器也支持已安装桌面应用的标准 `codex.exe` 路径，以及官方 npm 包的本地入口；它不会下载 CLI。

5. 在 Codex 中确认可以使用官方 **`$plugin-creator`** 技能。若安装器提示该技能缺失，请在 Codex 中使用它处理此源码文件夹，不要从不明来源下载替代安装脚本。

## 2. 安装用量计

1. 下载或克隆完整源码，将解压后的根文件夹命名为 **`codex-usage-meter`**。不要直接在压缩包预览中运行。
2. 审阅源代码后，双击 **`install-windows.cmd`**。它先检查已有的 `py -3`，再检查 `python`，只使用符合版本要求的解释器。
3. 也可以在该文件夹的 PowerShell 中执行：

   ```powershell
   .\install-windows.cmd
   ```

   若需要直接运行 Python 安装器：

   ```powershell
   py -3 -X utf8 .\scripts\install.py
   ```

   只有 `python` 命令可用时，将上述 `py -3` 换为 `python`。

4. 安装器先验证源码，通过官方 `plugin-creator` 辅助程序创建个人市场条目，复制插件并生成本机配置，再验证安装副本，最后调用 `codex plugin add`。
5. 看到安装完成提示后继续下一步。若已有同名插件目录，安装器会停止并保留现有文件。已有安装请使用官方插件更新流程，不要直接删除旧目录来重跑。

安装器不会直接修改 `marketplace.json` 或信任记录。它在**安装副本**中绑定实际 Python 路径、UTF-8 和独立数据目录；公开源码中的 `.mcp.json` 保持可移植状态。

## 3. 打开 Hooks 审阅

在 PowerShell 中进入已安装插件对应的 Codex 会话：

```powershell
codex -C "$env:USERPROFILE\plugins\codex-usage-meter"
```

如果命令不在 `PATH`，且你已安装的官方桌面应用确实提供以下文件，可以使用该文件：

```powershell
$meterCodex = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
& $meterCodex -C "$env:USERPROFILE\plugins\codex-usage-meter"
```

文件不存在时请返回第 1 步检查 CLI 安装，不要猜测其他可执行文件。文件夹信任、登录或组织策略提示均按 Codex 正常界面处理；不要为此修改执行策略或关闭保护。

- 若出现 **Hooks need review**，选择 **Review hooks**。
- 若没有出现，等 Codex 输入框就绪后，输入 `/hooks` 并按回车。

`/hooks` 要输入在 Codex 会话里，不是在普通 PowerShell 提示符后面。插件安装并不自动授予 Hooks 信任。[官方 Hooks 审阅说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

## 4. 只审阅并信任用量计的五项 Hooks

| 事件 | 用途 |
| --- | --- |
| `SessionStart` | 登记对话开始 |
| `UserPromptSubmit` | 登记新提问 |
| `Stop` | 登记本轮结束 |
| `SubagentStop` | 登记子代理轮次结束 |
| `Interrupt` | 登记本轮中断 |

使用方向键选择条目，按 **Enter** 查看详情。逐项确认来源为 **`codex-usage-meter`**，命令运行当前插件的 `scripts/meter.py`，使用 `hook` 子命令，并把记录写入用量计自己的数据目录。事件名称相同不代表来源相同。

Windows 安装副本使用 `commandWindows`：命令以 `& '实际的 Python 路径' -X utf8` 开始，通过 `$env:PLUGIN_ROOT` 定位当前插件。空格、中文和单引号均由安装器处理，不需要手工拼接。

- 当前待审阅列表**全部属于刚检查的用量计 Hooks**，且底部提示 `Press t to trust all` 时，才按字母 **`t`**，随后按界面提示确认。
- 若列表还包含其他插件或项目的 Hooks，只信任已经审阅的用量计条目，不要直接信任整个列表。

这里的 `t` 是 Codex 界面快捷键，不是 PowerShell 命令。不同版本的菜单文字可能有差异，以当前界面为准。更改后的 Hook 定义可能需要重新审阅。

## 5. 用新任务验证

1. 确认插件启用，用量计五项 Hooks 不再显示待审阅状态。
2. 回到 App，**新建一个任务**，发送「打开用量计」。新任务用于加载新安装的插件工具。
3. 使用工具返回的本地面板地址。再提交一个普通问题，确认该对话的记录和提问编号出现并随运行更新。
4. 若显示“部分记录”或“暂无估价”，保留提示含义：它们不是零消耗，也不能作为完整扣款账单。

## 文件位置与常见问题

| 内容 | 默认 Windows 位置 |
| --- | --- |
| 已安装的插件源码 | `%USERPROFILE%\plugins\codex-usage-meter` |
| 个人插件市场 | `%USERPROFILE%\.agents\plugins\marketplace.json` |
| 用量计运行数据 | `%LOCALAPPDATA%\Codex Usage Meter` |

`LOCALAPPDATA` 未提供时，数据目录回退到用户目录下的 `AppData\Local\Codex Usage Meter`。这些目录与 WSL 中的 Linux 用户目录不同。

**Python 或 Codex 提示找不到：** 新开 PowerShell 再检查版本与 `PATH`。安装器不会替你下载软件，也不会自动修改系统环境变量。

**提示无法导入 PyYAML：** 确认下载了完整源码，包括 `vendor` 文件夹；或在 Codex 内加载官方工作区依赖，再使用官方插件创建技能。无需由用量计在线安装依赖。

**脚本被组织策略或 PowerShell 执行策略阻止：** 保留现有策略，交由设备管理员或 Codex 官方支持处理，不使用跳过执行策略的参数。

**插件已安装但没有新记录：** 检查 Hooks 来源、信任状态和插件启用状态，再通过新任务测试。旧记录能打开不代表当前 Hooks 已运行。

**更新后重新要求信任：** 审阅新的定义，按上面的正常流程处理；安装器不代替你点击信任。

分享时只使用公开源码包。不要上传运行数据目录、带有本机路径的安装副本 `.mcp.json`、对话标题与别名缓存、账户额度或真实面板截图。

## 实现依据与验证边界

- [官方 Windows 文档](https://learn.chatgpt.com/docs/windows/windows-app)：原生代理使用 PowerShell；WSL 与原生环境分开配置。
- [官方 Hooks 文档](https://learn.chatgpt.com/docs/hooks)：`commandWindows` 覆盖项、`PLUGIN_ROOT`、事件及正常信任流程。
- [Codex Hooks 命令执行源码](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/engine/command_runner.rs)：Hook 命令由所配置的会话 shell 执行；本安装方式针对原生 PowerShell，不假定其他 shell 通用。

单元测试覆盖配置生成、中文与空格路径、安装流程的模拟及信任边界。Windows CI 还运行临时测试脚本，检查可用的 PowerShell 版本能否执行生成的 Hook 命令、原样传入 UTF-8 输入、保留路径与退出状态，并检查批处理启动器实际调用 Python；这些测试不会安装插件或访问账户。CI 通过不等于已在你的电脑上完成桌面插件、登录和 Hooks 的端到端验证，仍需完成上述新任务检查。
