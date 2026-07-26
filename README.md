# Podotion Image

Podotion Image 是一个可由 Codex Skills Install 直接安装的独立 Skill。它使用 Python 标准库调用 Podotion Images API，固定使用 `gpt-image-2`，支持生成、编辑、精确尺寸、请求恢复、双 SK 路由、诊断、更新和卸载。

Skill 的完整运行边界位于 `skills/podotion-image`。安装后不需要 Plugin Marketplace、MCP 注册或第三方 Python 包。

## 功能与取舍

- `auto`、1K、2K 和未达到 4K 标准的精确尺寸使用 `PodotionImageSk`。
- 显式 4K 档、规范 4K 档尺寸或总像素达到 8,294,400 时使用 `PodotionImage4kSk`。
- 旧单 SK 配置继续支持非 4K 请求；4K SK 缺失时在联网和写请求状态前失败，不回退默认 SK。
- 用户给出的合法精确像素尺寸会原样发送，不映射到最近档位。
- 每次上游请求固定 `n=1` 且只接受一张标准 `data[]` 结果；每个图片操作最多发送一次 POST，不自动重试，请求超时固定为 600 秒。
- 用户需要多张图片时由 Skill 最多串行执行 10 个独立图片操作，绝不并行；任一操作失败或结果不确定时立即停止，保留此前成功图片。
- 图片保存在用户工作区，并在回复正文中提供 Markdown 预览和绝对文件链接。
- `doctor` 只做配置和连通性检查，不计费；`doctor --image-probe` 会真实生图并可能计费。

纯 Skill 不再提供 MCP `image`、`resource_link`、`resources/list`、`resources/read`、`publish_existing_image` 或 Codex Outputs 面板登记。这是使用标准 Skills Install 和保持目录自包含所需的明确取舍；PNG、正文预览和本地文件链接仍会保留。

## 为什么旧版不能 Skills Install

标准 Skill 安装器只校验并复制指定的 Skill 目录到 `$CODEX_HOME/skills/<name>`。它不会执行安装钩子，不会复制仓库根目录代码，也不会注册 `.mcp.json` 或个人 Marketplace。

旧版存在三处硬依赖：

- `SKILL.md` 要求调用只由 Plugin 注册的 MCP 工具。
- Skill 执行器导入仓库根部的 `podotion_image.paths`，单独复制后无法启动。
- 根 `scripts/install.py` 安装的是完整 Plugin 并修改 Marketplace，不属于 Skills Install 流程。

本次重构把运行脚本、配置模板和生命周期工具全部收进 Skill 子目录，并删除 Plugin/MCP 安装面。标准安装 URL 必须包含 Skill 子路径；裸仓库 URL 无法确定要安装哪个目录。

## 系统要求

- Codex Desktop、Codex CLI 或 IDE extension。
- Python 3.11 或更高版本，仅使用标准库，无需 `pip install`。
- 生图和 `doctor` 需要访问 `https://ai.podotion.com/v1`。
- 首次安装需要访问 GitHub；内置更新还需要 Git。
- 两个 SK 所属分组按用途启用 `gpt-image-2`。

安装 Codex 不代表系统一定提供可由普通 Skill 调用的 Python。Windows 可安装官方 Python 后使用 `py -3`；macOS、Linux 和 WSL 使用原生 `python3`。不要跨 Windows 和 WSL 混用解释器。

## 使用 Skills Install 首次安装

将下面两行发给 Codex；安装阶段不提交或配置任何 SK：

```text
$skill-installer
https://github.com/scp-planet/podotion-image/tree/main/skills/podotion-image
```

安装完成后重启 Codex 并新建任务。不要在安装任务中发送 SK、配置凭据、运行 `doctor` 或迁移旧 Plugin；这些动作会在首次真实图片任务中按需处理。安装器如果报告目标目录已存在，停止首次安装并改用已安装 Skill 的 `manage.py update`，不要覆盖目录。

标准 `skill-installer` 对公开 GitHub 仓库默认可直接下载 ZIP，并在需要时使用它自己的 Git fallback。首次安装的传输行为由系统安装器管理；下文的固定浅克隆和 Schannel 策略专用于安装后的自更新。

## 首次图片任务预检与凭据

每个新任务的第一次生成或编辑之前，Skill 先运行本地、只读的 `configure_direct.py --check` 和 `manage.py status`。预检不访问 Podotion、不生成图片、不写文件，也不输出 SK；同一任务后续图片操作不重复运行。

- 如果默认凭据尚未配置，Skill 先索取 `PodotionImageSk`，不发起图片请求。
- 如果用户请求 4K 而配置中缺少 4K 凭据，Skill 只索取 `PodotionImage4kSk`；非 4K 请求不要求 4K SK。
- 如果检测到可安全自动清理的旧 Plugin，Skill 在每个新任务首次预检时询问是否迁移。用户本任务拒绝后可以继续使用 standalone Skill，但下一个新任务仍会再次询问，直到迁移完成；检测结果不安全或不明确时只报告脱敏状态，不询问或尝试清理。

凭据可以用以下任一方式提供：

- 在当前消息中写一行 `PodotionImageSk=<value>` 或 `PodotionImage4kSk=<value>`。
- 附加一个 UTF-8 文本文件，内容使用相同的 `KEY=value` 行；同时配置两个 profile 时每个 key 各占一行，且不包含其他字段。

附件方式避免把 SK 字面量放进聊天提示正文或进程命令行参数，但附件内容仍是当前 Codex 会话的输入，并不是会话外的秘密通道。正文中的赋值块会原样通过配置器 stdin 解析；附件有本地路径时使用 `--input-file` 直接读取，没有路径时才把附件内容通过 stdin 传递。Skill 不在回复、命令参数或日志中复述 SK，也不会扫描其他文件。

配置文件位于 `$CODEX_HOME/podotion-image/provider.toml`；`CODEX_HOME` 未设置时使用当前原生平台的 `~/.codex/podotion-image/provider.toml`。该文件在 Skill 安装目录和 Git 仓库之外。

规范配置包含：

```toml
base_url = "https://ai.podotion.com/v1"
PodotionImageSk = "<default>"
PodotionImage4kSk = "<4k>"
```

旧文件只有 `PodotionImageSk` 时会被原位读取，不需要复制或扫描 Plugin cache。正文配置把一个或两个 `KEY=value` 赋值行通过 stdin 交给：

```text
configure_direct.py --stdin --force
```

按需补充 4K SK 时，把 `PodotionImage4kSk=<value>` 作为一行标准输入传给：

```text
configure_direct.py --set-4k --stdin --force
```

附件配置使用 `configure_direct.py --input-file <attachment-path> --force`；仅补充 4K 时再加 `--set-4k`。配置写入使用同目录临时文件和原子替换，附件原文件不会被修改或删除。

任何成功的配置写入后，Skill 都会在当前任务运行不带 `--image-probe` 的非计费 `doctor`；doctor 成功且用户已经同意迁移时，再自动清理旧 Plugin。完成配置和可选迁移后停止当前任务，提示用户重启 Codex 并新建任务，不继续原始生图。doctor 失败时不迁移、不生图，只报告脱敏错误。

## 分辨率与 SK 路由

`gpt-image-2` 接受满足以下条件的任意 `WIDTHxHEIGHT`：

- 最长边不超过 3840 像素。
- 宽和高都是 16 的倍数。
- 长边与短边之比不超过 3:1。
- 总像素不少于 655,360 且不超过 8,294,400。

来源：[OpenAI Image generation](https://developers.openai.com/api/docs/guides/image-generation) 与 [gpt-image-2 size options](https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide#gpt-image-2-size-options)。

`gpt-image-2` 在上述限制内支持任意 `WIDTHxHEIGHT`，但大于 2K 的输出仍属于实验性能力。这里的 `4K` 表示“OpenAI 限制内、符合目标比例的最高分辨率档”，不是在所有比例下都强制使用某个固定边长。只有 `3840x2160` 和 `2160x3840` 是 UHD 4K；电影级 DCI 4K `4096x2160` 超过 3840 像素边长限制，不能直接请求，也不会被静默缩小。

用户只给出“4K + 比例”时，Skill 使用以下固定映射：

| 比例 | OpenAI 兼容的 4K 档尺寸 |
| --- | --- |
| `1:1` | `2880x2880` |
| `2:3` | `2336x3504` |
| `3:2` | `3504x2336` |
| `3:4` | `2448x3264` |
| `4:3` | `3264x2448` |
| `16:9` | `3840x2160`（UHD 4K） |
| `9:16` | `2160x3840`（UHD 4K） |
| `19:10` | `3648x1920` |
| `10:19` | `1920x3648` |

“电影比例”“DCI 比例”和 `1.90:1` 作为粗略比例意图时按 `19:10` 处理；精确请求 `4096x2160` 仍会因越界而拒绝。字面比例 `19:6` 超过 3:1 限制，不受支持。

| 用户意图或最终尺寸 | 请求形式 | SK |
| --- | --- | --- |
| 未指定尺寸 | `auto` | `PodotionImageSk` |
| 1K、2K 或合法中间尺寸 | 档位或精确尺寸 | `PodotionImageSk` |
| 明确 4K 档 | `4k` 加比例 | `PodotionImage4kSk` |
| 4K 档解析出的规范尺寸 | 精确尺寸 | `PodotionImage4kSk` |
| 总像素达到 8,294,400 | 精确尺寸 | `PodotionImage4kSk` |

例如 `2560x1440` 使用默认 SK，`3840x2160` 使用 4K SK。路由由执行器根据结构化尺寸意图选择；Codex 不读取或拼接密钥。

## 使用

安装并重启后直接描述任务。Skill 会先完成本任务唯一一次本地预检；若需要写入凭据，它会先完成非计费 doctor 和已授权的旧版迁移，再停止并要求用户重启、新建任务、重新提出图片请求：

```text
生成一张 2560x1440 的绿色山谷头图，保存到 assets/generated。
```

```text
生成一张 4K、16:9 的产品海报，然后把上一张图的背景改成白色。
```

相对输出目录以活动项目 workspace 为基准；无项目任务使用 conversation workspace；未指定时使用 `<workspace>/PodotionImageOutput`。存在多个合理路径或编辑源时，Skill 会在计费请求前询问。

同一任务复用稳定的 `state_scope`，每个独立图片操作使用新的 `request_key`。断连后使用同一组标识运行 `request-status`，不要换 key 重发。只有用户明确承认请求可能已计费时，才运行 `request-abandon --acknowledge-possible-charge`。

每次 `generate` 或 `edit` 都显式请求 `n=1`。执行器只读取标准 Images API 顶层 `data` 数组，并要求它恰好包含一个图片对象；同一对象优先使用 `b64_json`，仅在缺失时才使用 `url`。不会扫描非标准 `images`、嵌套 `response` 或把同一对象的两个表示当成两张图片。响应数量不符合单图契约时，在保存 PNG 前按不可用结果停止，且不会自动重试。

需要多张图片时，请明确给出 1 到 10 的数量。Skill 会在首次计费调用前一次性确认所有图片的提示词、尺寸、凭据路由和输出目录，然后逐张串行执行：上一张完成解码、原子保存和请求状态落盘后才开始下一张。每张使用独立 `request_key`；相同提示词的第二张及以后使用 `--force-new`，避免复用第一张。任一张失败、断连或状态不确定时，后续图片不再执行，已成功图片继续保留并在回复中列出。

## 状态、更新和卸载

这些操作可通过自然语言请求 Skill 执行，也可手动运行 `scripts/manage.py`。

```text
manage.py status
manage.py update --dry-run
manage.py update
manage.py uninstall-legacy-plugin --yes
manage.py uninstall --yes
```

更新固定浅克隆 `https://github.com/scp-planet/podotion-image.git` 的 `main`，不接受自定义来源、分支或路径。候选会校验 Skill 名称、必需文件、Python 语法、隔离启动、符号链接和内容摘要，然后在 `$CODEX_HOME` 同卷事务目录中替换安装目录；捕获到替换异常时恢复旧版本。

仅在原生 Windows 的 Git 输出明确是 Schannel TLS 错误时，更新器才以单条 `git -c http.sslBackend=openssl clone ...` 重试。它不会修改持久 Git 配置、切换所有 Git 请求或关闭证书校验。

`uninstall-legacy-plugin --yes` 用于从旧 Plugin 迁移。每个新任务的首次图片预检只要仍检测到可安全清理的旧 Plugin，就会询问用户是否迁移；拒绝只对当前任务有效。用户同意后，Skill 必须先在本任务运行不计费的 `podotion_image.py doctor`，且不得使用 `--image-probe`。只有 doctor 成功才运行清理；doctor 失败时保留旧 Plugin 并停止迁移。

清理命令读取旧 Marketplace 名称并调用对应的 `codex plugin remove podotion-image@<marketplace>`（通常是 `podotion-image@personal`），只删除旧安装器创建的精确 Marketplace 条目和 `~/plugins/podotion-image` source；同名但来源不同的条目会被拒绝。旧 Plugin 不存在时命令安全跳过。凭据、新 Skill、工作区图片和请求状态不在清理范围内。迁移成功后立即停止当前任务，提示重启 Codex 并新建任务，不在旧任务中继续生图。

`uninstall --yes` 只移除 `$CODEX_HOME/skills/podotion-image`。凭据、工作区图片和请求状态一律保留；v1 不提供 purge。更新或任一卸载操作后必须重启 Codex 并新建任务。

## 从旧 Plugin 迁移

旧 Plugin 和 standalone Skill 使用相同凭据路径，因此新 Skill 会自动复用已有默认 SK。安装提示词只安装 Skill，不接收凭据，也不清理旧 Plugin。重启后的每个新任务会在首次图片预检中检测旧 Plugin 并询问是否迁移。

用户同意迁移时，Skill 在当前任务运行非计费 `doctor`；不能依赖另一个任务中的旧结果。若同时新增或修改凭据，则先原子写入配置，再运行 doctor。doctor 成功后才调用 `manage.py uninstall-legacy-plugin --yes`；配置或迁移完成后停止并要求重启、新建任务，不执行原始生图。

迁移命令会保留个人 Marketplace 中的其他 Plugin，并拒绝删除任何同名但 source 不是 `./plugins/podotion-image` 的条目。用户不需要手工运行 `codex plugin remove`、编辑 `marketplace.json` 或删除旧 source。完成后只需重启 Codex 并新建任务。

## 项目结构与开发

```text
skills/podotion-image/
  SKILL.md
  agents/openai.yaml
  evals/evals.json
  scripts/
    configure_direct.py
    manage.py
    podotion_image.py
  templates/provider.toml
```

在仓库根目录运行：

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m compileall -q skills\podotion-image\scripts tests
py -3 "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" skills\podotion-image
```

macOS、Linux 和 WSL 将 `py -3` 替换为 `python3` 并使用 POSIX 路径。发布前还要把 Skill 子目录单独复制到临时目录，以 `python -I` 验证三个脚本，无需也不得执行计费生图。
