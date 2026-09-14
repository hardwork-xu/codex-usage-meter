# 用量计首次启用：一步一步操作

适用于 macOS 上的用量计 0.4.0。**插件安装完成后，自动记录还需要你手动审阅并信任 Hooks。** Hooks 是在开始提问、结束提问等时刻运行的小程序；用量计用它们登记本地统计来源。

如果插件已经安装，直接从第 2 步开始。下文的 `$HOME` 会自动代表你自己的用户目录，不需要替换成姓名。

## 第 1 步：安装插件

先确认已有 Python 3.10+、已登录的 Codex，以及官方 `plugin-creator` 技能。详细要求见 [README](../README.md#安装与使用)。

1. 下载或克隆仓库，将解压后的文件夹命名为 `codex-usage-meter`。
2. 审阅源码后，运行文件夹里的 **安装到 Codex.command**。也可以在该文件夹的终端中执行 `python3 scripts/install.py`。
3. 等待安装器显示插件已添加。默认安装位置为 `~/plugins/codex-usage-meter`。

如果提示已有同名文件夹，说明需要检查现有安装；不要为了重跑安装器直接删除它。已有本地插件应通过 Codex 的插件更新流程处理。

## 第 2 步：在终端打开 Codex

1. 按 **⌘＋空格**，搜索「终端」，按回车打开。
2. 如果终端已经能运行 `codex`，复制下面一行，粘贴后按回车：

   ```sh
   codex -C "$HOME/plugins/codex-usage-meter"
   ```

   如果出现 `command not found: codex`，并且桌面应用安装在 `/Applications/ChatGPT.app`，改用它自带的 Codex：

   ```sh
   /Applications/ChatGPT.app/Contents/Resources/codex -C "$HOME/plugins/codex-usage-meter"
   ```

   如果应用安装在 `/Applications/Codex.app`，则使用对应路径：

   ```sh
   /Applications/Codex.app/Contents/Resources/codex -C "$HOME/plugins/codex-usage-meter"
   ```

3. 如果出现文件夹信任提示，核对当前目录是刚安装的 `plugins/codex-usage-meter`，然后按正常提示决定是否信任该文件夹。

以上命令只用于打开 Codex 的本地界面。下面的 `/hooks` 也要输入在这个终端中的 Codex 输入框里。

## 第 3 步：进入 Hooks 审阅界面

如果启动时出现 **Hooks need review**，菜单通常包含：

| 选项 | 含义 |
| --- | --- |
| `1. Review hooks` | 查看待审阅钩子 |
| `2. Trust all and continue` | 信任所有待审阅钩子并继续 |
| `3. Continue without trusting (hooks won't run)` | 继续使用，但这些钩子不会运行 |

先选择 **`1. Review hooks`**，按回车。如果光标已经在这一项上，直接按回车即可。

如果没有出现这个菜单，等 Codex 输入框出现后，输入下面内容并按回车：

```text
/hooks
```

如果之前选择了第 3 项，也可以通过 `/hooks` 重新进入审阅界面。

## 第 4 步：审阅并信任用量计的五项钩子

用量计 0.4.0 配置了以下五项：

| 名称 | 登记时机 |
| --- | --- |
| `SessionStart` | 对话开始 |
| `UserPromptSubmit` | 提交问题 |
| `Stop` | 当前轮次结束 |
| `SubagentStop` | 子代理轮次结束 |
| `Interrupt` | 当前轮次被中断 |

按上下方向键选择事件，按 **Enter／回车** 查看详情。审阅时确认来源属于 `codex-usage-meter`，命令指向该插件的 `scripts/meter.py`，并使用 `hook` 子命令登记本地统计来源。**仅有相同事件名称不能证明来源是用量计。** 按当前界面提示返回或继续审阅其余项目。

完成审阅后：

- 如果列表中所有待信任项目都属于用量计的这五项，且底部显示 `Press t to trust all`，直接按字母 **`t`**；如果随后出现确认提示，再确认信任。
- 如果还有其他来源的待审阅钩子，进入各项详情，只对你已经审阅的用量计项目执行信任操作。

`t` 是界面快捷键，不需要按 ⌘。这里的「全部」作用于当前待审阅列表，因此应先确认列表来源。

## 第 5 步：确认成功，回到 App 打开面板

Hooks 表格中，`Installed` 表示已安装数量，`Active` 表示可运行数量，`Review` 表示待审阅数量。如果当前只有用量计这五项钩子，且插件保持启用、五项都已信任，它们对应的 `Active` 应为 `1`、`Review` 应为 `0`。如果所有待审阅项目都已处理，黄色的 `5 hooks need review` 提示也应消失。

然后回到 **Codex App**，新开一个对话，发送：

```text
打开用量计
```

新对话用于加载已安装插件的工具。面板打开后，可以选择对话标题、查看提问序号，或设置本地别名。新问题产生统计记录后，面板会刷新用量；这才是自动登记是否运行的实际验证。

## 常见问题

**插件显示已安装，为什么没有新对话记录？**

安装与 Hooks 信任是不同步骤。先在 `/hooks` 确认用量计的 `Review` 为 `0`，并保持插件启用，再用新对话测试。已有面板里的旧记录可以来自先前登记，不能单凭它们判断新任务是否已自动登记。

**更新后为什么又提示审阅？**

Codex 将信任绑定到具体钩子定义；新增或更改后的钩子可能需要重新审阅。这时重复正常审阅流程，不应修改信任记录或使用跳过检查的参数。

**界面和本文不一样怎么办？**

本文菜单和快捷键依据 Codex CLI `0.154.0-alpha.6.2` 的界面整理。其他版本请以屏幕提示及[官方 Hooks 说明](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)为准。如果需要向他人求助，优先描述选项文字；发送截图前裁剪或遮挡用户名、机器名、对话内容和账户额度等私人信息。

**分享或提交源码时，哪些内容不应上传？**

使用仓库里的公开源代码或源码包。不要上传 `~/Library/Application Support/Codex Usage Meter` 中的运行数据，也不要把带有本机路径的已安装 `.mcp.json`、真实任务记录、标题缓存、别名、账户用量或实际面板截图复制回仓库。本文仅使用通用路径和文字说明。

## 官方依据

- [审阅与信任 Hooks](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)：`/hooks` 入口、对具体定义的信任，以及变更后的重新审阅。
- [插件 Hooks](https://learn.chatgpt.com/docs/hooks)：安装或启用插件后，钩子仍遵循正常信任流程。
