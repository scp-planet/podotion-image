# Podotion Image

Podotion Image 是一个可由 Codex Skills Install 直接安装的独立 Skill。它使用 Python 标准库调用 Podotion Images API，固定使用 `gpt-image-2`，支持生成、编辑、精确尺寸、请求恢复、双 SK 路由、诊断、更新和卸载。

Skill 的完整运行边界位于 `skills/podotion-image`。安装后不需要 Plugin Marketplace、MCP 注册或第三方 Python 包。

## 功能与取舍

- `auto`、1K、2K 和未达到 4K 标准的精确尺寸使用 `PodotionImageSk`。
- 显式 4K 档、规范 4K 档尺寸或总像素达到 8,294,400 时使用 `PodotionImage4kSk`。
- 旧单 SK 配置继续支持非 4K 请求；4K SK 缺失时在联网和写请求状态前失败，不回退默认 SK。
- 用户给出的合法精确像素尺寸会原样发送，不映射到最近档位。
- 每个图片操作最多发送一次上游 POST，不自动重试；请求超时固定为 600 秒。
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

将下面整段提示词发给 Codex，并替换两个占位符：

```text
请使用内置 skill-installer 从 https://github.com/scp-planet/podotion-image/tree/main/skills/podotion-image 安装 podotion-image Skill。不要安装整个仓库，也不要注册 Plugin 或 MCP。安装器如果报告目标目录已存在，停止首次安装并改用已安装 Skill 的 manage.py update，不要覆盖目录。

安装后使用当前平台原生 Python 运行安装目录中的 scripts/configure_direct.py --stdin --force：原生 Windows 使用 py -3，macOS、Linux 和 WSL 使用 python3。仅把下面两行通过该进程的标准输入传入，不得把 SK 放入命令行参数、日志或回复；第一行是默认 SK，第二行是 4K SK：
{{PodotionImageSk}}
{{PodotionImage4kSk}}

然后运行不计费的 scripts/podotion_image.py doctor，不得运行 --image-probe。doctor 成功后，运行 scripts/manage.py uninstall-legacy-plugin --yes；该命令负责检测并卸载旧的 podotion-image@personal Plugin，未检测到旧 Plugin 时应安全跳过，不要要求我手工编辑 Marketplace 或删除目录。最后只报告脱敏状态，并提示我重启 Codex、新建任务。
```

标准 `skill-installer` 对公开 GitHub 仓库默认可直接下载 ZIP，并在需要时使用它自己的 Git fallback。首次安装的传输行为由系统安装器管理；下文的固定浅克隆和 Schannel 策略专用于安装后的自更新。

## 凭据与旧配置

配置文件位于 `$CODEX_HOME/podotion-image/provider.toml`；`CODEX_HOME` 未设置时使用当前原生平台的 `~/.codex/podotion-image/provider.toml`。该文件在 Skill 安装目录和 Git 仓库之外。

规范配置包含：

```toml
base_url = "https://ai.podotion.com/v1"
PodotionImageSk = "<default>"
PodotionImage4kSk = "<4k>"
```

旧文件只有 `PodotionImageSk` 时会被原位读取，不需要复制或扫描 Plugin cache。补充 4K SK 时，使用隐藏交互输入，或把一行 SK 通过 stdin 传给：

```text
configure_direct.py --set-4k --stdin --force
```

全量配置仍使用 `configure_direct.py --stdin --force`：第一行默认 SK，第二行可选 4K SK。配置写入使用同目录临时文件和原子替换；状态、doctor 和错误输出不会包含 SK。

## 分辨率与 SK 路由

`gpt-image-2` 接受满足以下条件的任意 `WIDTHxHEIGHT`：

- 最长边不超过 3840 像素。
- 宽和高都是 16 的倍数。
- 长边与短边之比不超过 3:1。
- 总像素不少于 655,360 且不超过 8,294,400。

来源：[OpenAI Image generation](https://developers.openai.com/api/docs/guides/image-generation)。

| 用户意图或最终尺寸 | 请求形式 | SK |
| --- | --- | --- |
| 未指定尺寸 | `auto` | `PodotionImageSk` |
| 1K、2K 或合法中间尺寸 | 档位或精确尺寸 | `PodotionImageSk` |
| 明确 4K 档 | `4k` 加比例 | `PodotionImage4kSk` |
| 4K 档解析出的规范尺寸 | 精确尺寸 | `PodotionImage4kSk` |
| 总像素达到 8,294,400 | 精确尺寸 | `PodotionImage4kSk` |

例如 `2560x1440` 使用默认 SK，`3840x2160` 使用 4K SK。路由由执行器根据结构化尺寸意图选择；Codex 不读取或拼接密钥。

## 使用

安装并重启后直接描述任务：

```text
生成一张 2560x1440 的绿色山谷头图，保存到 assets/generated。
```

```text
生成一张 4K、16:9 的产品海报，然后把上一张图的背景改成白色。
```

相对输出目录以活动项目 workspace 为基准；无项目任务使用 conversation workspace；未指定时使用 `<workspace>/PodotionImageOutput`。存在多个合理路径或编辑源时，Skill 会在计费请求前询问。

同一任务复用稳定的 `state_scope`，每个独立图片操作使用新的 `request_key`。断连后使用同一组标识运行 `request-status`，不要换 key 重发。只有用户明确承认请求可能已计费时，才运行 `request-abandon --acknowledge-possible-charge`。

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

`uninstall-legacy-plugin --yes` 用于从旧 Plugin 迁移。它读取旧 Marketplace 名称并调用对应的 `codex plugin remove podotion-image@<marketplace>`（通常是 `podotion-image@personal`），只删除旧安装器创建的精确 Marketplace 条目和 `~/plugins/podotion-image` source；同名但来源不同的条目会被拒绝。旧 Plugin 不存在时命令安全跳过。凭据、新 Skill、工作区图片和请求状态不在清理范围内。

`uninstall --yes` 只移除 `$CODEX_HOME/skills/podotion-image`。凭据、工作区图片和请求状态一律保留；v1 不提供 purge。更新或任一卸载操作后必须重启 Codex 并新建任务。

## 从旧 Plugin 迁移

旧 Plugin 和 standalone Skill 使用相同凭据路径，因此新 Skill 会自动复用默认 SK。使用上方安装提示词即可完成迁移：它安装 standalone Skill、写入双 SK、运行非计费 `doctor`，然后调用 `manage.py uninstall-legacy-plugin --yes` 清理旧 Plugin。

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
