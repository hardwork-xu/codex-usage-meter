# 从 GitHub 安装用量计

新用户可以直接把本仓库加入 Codex 的插件来源，再安装用量计。适用于 macOS 和原生 Windows；不需要先下载源码、安装 `plugin-creator`，或手工修改 MCP 配置。

这是社区仓库提供的插件来源，**不是 OpenAI 官方发布、认证或上架到官方插件目录的插件**。已经通过 `personal` 个人市场安装的用户，请保留原有渠道，不要再重复安装这一份。

## 1. 准备环境

使用已登录的 Codex CLI，并更新到支持 `plugin` 子命令的版本。在 macOS 终端或 Windows PowerShell 中检查：

```sh
codex --version
codex plugin --help
```

如果提示找不到 `codex`，或不认识 `plugin`，先按 [Codex CLI 官方说明](https://learn.chatgpt.com/docs/cli)安装或更新，再打开新的终端。按 Codex 的正常流程完成登录。

还需要 Python 3.10 或更新版本，并让对应命令在 Codex 使用的环境中可用：

| 系统 | 检查命令 |
| --- | --- |
| macOS | `python3 --version` |
| 原生 Windows | `py -3 --version` |

Windows 使用 PowerShell，不需要 WSL。原生 Windows 与 WSL 的 Python、插件和数据目录是不同环境，不要混用。

## 2. 添加来源并安装

先审阅 [仓库源码](https://github.com/hardwork-xu/codex-token-usage-dashboard)，包括插件技能、本地脚本和 Hooks。然后依次执行：

```sh
codex plugin marketplace add hardwork-xu/codex-token-usage-dashboard
codex plugin add codex-usage-meter@codex-usage-meter-community
```

第一条命令登记仓库提供的 `codex-usage-meter-community` 来源；第二条安装其中的用量计。这个来源使用仓库的 `main` 分支。来源登记和更新使用 Codex 的正常插件命令，不需要编辑 `config.toml`。[官方插件打包与市场说明](https://developers.openai.com/plugins/build/plugins)

命令完成后，仍需按下一步审阅 Hooks。安装插件本身不代表它的 Hooks 已获信任。

## 3. 审阅并信任五项 Hooks

在终端启动 Codex，按正常界面处理登录、文件夹权限等提示。等 Codex 输入框就绪后输入 `/hooks`；这是 **Codex 会话中的命令**，不是普通终端命令。看到 `Hooks need review` 时也可以进入 `Review hooks`。

逐项查看以下五个事件的来源与命令：

| 事件 | 用途 |
| --- | --- |
| `SessionStart` | 登记任务开始 |
| `UserPromptSubmit` | 登记新提问 |
| `Stop` | 登记本轮结束 |
| `SubagentStop` | 登记子任务结束 |
| `Interrupt` | 登记本轮中断 |

确认来源属于刚安装的 **`codex-usage-meter`**，命令只运行该插件的本地记录脚本。事件名称相同并不代表来源相同。

- 如果待审阅列表全部属于你刚检查的用量计，且界面显示 `Press t to trust all`，才按 **t** 并完成界面确认。
- 如果还有其他来源的待审阅项，只处理已经审阅的用量计条目。

用量计不会替你点击信任，也不会调整沙箱或审批规则。定义发生变化后可能需要重新审阅。[官方 Hooks 信任说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)

## 4. 在新任务中打开面板

回到 Codex App，**新建一个任务**，发送：

> 打开用量计。

插件技能会定位当前安装版本，并在正常权限范围内运行本地入口：macOS 使用 `python3`，Windows 使用 `py -3`，执行插件中的 `scripts/run.py open`。你不需要寻找缓存目录或把其中的路径写进配置文件。

打开返回的本地面板地址。面板提供每次提问、今天和当前订阅周期的用量，按模型列出 Token，并支持人民币、美元、港元。订阅周期需要在设置中填写自己的每月续订日。

新提问发生后，检查对应任务是否出现记录。大日志会逐步补读；“部分统计”“尚未补齐”“暂无估价”分别保留其含义，不能当成零消耗或完整账单。只覆盖已登记任务，未提供可靠费率的模型仍显示 Token。

## 5. 可选：macOS 按需浏览器入口

希望之后直接访问保存的面板地址，可以在安装后的任务中说：

> 请启用用量计的 macOS 按需浏览器入口，并告诉我固定的本地面板地址。

插件技能会定位并调用 `scripts/run.py browser-install`。此入口只适用于 macOS，按当前用户权限安装；浏览器访问保存的地址时才启动统计进程，空闲后退出，下次访问再次唤醒。它不代替 Hooks 审阅，也不会启动模型任务。

后续可以自然语言要求“查看用量计按需入口状态”或“卸载用量计按需入口”，对应 `scripts/run.py browser-status` 和 `scripts/run.py browser-uninstall`。卸载入口会保留任务数据和面板地址。Windows 继续通过“打开用量计”启动，不执行这些 macOS 服务管理命令。

## 6. 更新与常见问题

**仅适用于本指南安装的 GitHub 渠道**：更新时执行以下两条命令，然后新建任务加载新版本：

```sh
codex plugin marketplace upgrade codex-usage-meter-community
codex plugin add codex-usage-meter@codex-usage-meter-community
```

如果新版 Hooks 提示待审阅，重新按第 3 步检查当前定义。更新来源不是自动授予信任。已经使用 `personal` 渠道的用户继续使用原有更新方式，不要为了更新再安装 GitHub 渠道。

如果已经启用 macOS 按需入口，更新后在新任务中要求“请更新用量计的按需浏览器入口”。技能会再次执行 `browser-install`，刷新入口使用的本地运行副本，并保留已有数据与面板地址。

**新任务仍找不到插件：** 检查安装命令是否成功、插件是否启用，以及 App 和 CLI 是否使用同一个用户环境。可用 `codex plugin marketplace list` 检查是否已登记本指南的来源；不要通过手改配置文件强行加载。

**提示 Python 不存在或版本过低：** 返回第 1 步检查对应平台命令；插件不会替你在线下载解释器。

**面板能打开但没有新记录：** 检查这五项 Hooks 的来源、启用和信任状态，再用新任务验证。能打开旧面板不等于当前 Hooks 已在运行。

**提示权限或组织策略限制：** 按 Codex 正常授权流程处理；不要让安装请求绕过审批、Hooks 信任或系统保护。

如果更习惯让 Codex 协助，可复制这一句作为安装请求：

> 请先审阅 https://github.com/hardwork-xu/codex-token-usage-dashboard 的源码并检查是否已有 personal 安装；如果没有，请通过官方插件命令安装 codex-usage-meter@codex-usage-meter-community，保留正常权限与 Hooks 审阅流程，告诉我需要手动确认的步骤，不要让我手改 config.toml 或 MCP 配置。

手工源码安装与旧 `personal` 渠道说明仍保留在 [macOS 指南](SETUP.zh-CN.md) 和 [Windows 指南](SETUP.windows.zh-CN.md)。这条兼容路径使用原有安装器，与本指南的 GitHub 直接安装二选一。
