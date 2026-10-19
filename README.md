# Podotion Image

Podotion Image 是一个可由 Codex Skills Install 直接安装的独立 Skill。它使用 Go 原生 CLI 调用 Podotion Images API，支持生成、编辑、精确尺寸、请求恢复、双 SK 路由、诊断、更新、旧 Plugin 迁移和卸载。

用户不需要安装 Python、Node、Git 或其他运行时。Skill 首次运行时会从固定 GitHub Releases 下载与当前 OS/架构匹配的静态二进制，校验 SHA-256 后缓存到：

```text
$CODEX_HOME/podotion-image/runtime/<version>/<os>-<arch>/
```

生图模型可配置。全新配置默认使用 `gpt-image-2.5`；已有配置不会被安装或更新流程修改。

## 主要变化

- **去 Python 化**：正式运行路径不再包含 Python 脚本，也不要求用户安装解释器。
- **原生 CLI**：Windows、macOS、Linux 的 amd64 与 arm64 均提供 `CGO_ENABLED=0` 静态二进制。
- **模型可配置**：支持 `provider.toml` 的 `model` 字段、单次 `--model` 覆盖和 `config set-model`；全新配置默认 `gpt-image-2.5`。
- **原生更新**：`update` 直接通过 HTTPS 下载 GitHub ZIP，不再依赖 Git。
- **轻量 Skill**：Skill 目录只保留 `SKILL.md`、`agents/openai.yaml`、`runtime.json` 和 `bin/` 启动器。

## 功能与取舍

- `auto`、1K、2K 和未达到 4K 标准的精确尺寸使用 `PodotionImageSk`。
- 显式 4K 档、规范 4K 档尺寸或总像素达到 8,294,400 时使用 `PodotionImage4kSk`。
- 旧单 SK 配置继续支持非 4K 请求；4K SK 缺失时在联网和写请求状态前失败，不回退默认 SK。
- 旧 `provider.toml` 没有 `model` 字段时继续使用 `gpt-image-2`，升级不会改变现有用户行为。
- 模型优先级固定为：`--model` > `provider.toml` 的 `model` > 新配置默认 `gpt-image-2.5`（旧配置缺省为 `gpt-image-2`）。
- 不根据模型名自动推断能力；尺寸、4K 和双 SK 规则保持不变。未知模型由上游报错。
- 用户给出的合法精确像素尺寸会原样发送，不映射到最近档位。
- 每次上游请求固定 `n=1`，从标准 `data[]` 或明确兼容的 `output[]`、`response.output[]` 中只接受一张最终结果；每个图片操作最多发送一次 POST，不自动重试，请求超时固定为 600 秒。
- 只使用同步 Images API：生成调用 `POST /v1/images/generations`，编辑调用 `POST /v1/images/edits`；不调用 `/async`，不发送 `stream=true`，不轮询异步任务。
- 多图请求由 Skill 最多串行执行 10 个独立图片操作，绝不并行；每生成并保存一张就立即返回该张结果。
- 图片保存在用户工作区，并在回复正文中提供 Markdown 预览和绝对文件链接。
- `doctor` 只做配置和连通性检查，不计费；`doctor --image-probe` 会真实生图并可能计费。

Skill 不提供 MCP `image`、`resource_link`、`resources/list`、`resources/read`、`publish_existing_image` 或 Codex Outputs 面板登记。PNG、正文预览和本地文件链接仍然保留。

## 安装与首次运行

使用标准 Skills Install 安装 `skills/podotion-image` 后，重启 Codex 并新建任务。

首次执行任意图片、诊断或配置命令时：

1. 启动器读取 Skill 目录中的 `runtime.json`。
2. 按当前 OS/架构选择固定 GitHub Release 资源。
3. 下载二进制及其 `.sha256` 文件。
4. 校验 SHA-256 后写入版本化缓存目录。
5. 运行缓存中的二进制。

缓存命中后不会重复下载。更新会使用新的版本目录，避免 Windows 覆盖正在运行的可执行文件。未签名的 macOS/Windows 二进制可能触发系统安全提示；后续可增加代码签名。

## 配置

用户配置入口只有：

```text
$CODEX_HOME/podotion-image/provider.toml
```

示例：

```toml
base_url = "https://ai.podotion.com/v1"
model = "gpt-image-2.5"
PodotionImageSk = "sk-..."
PodotionImage4kSk = "sk-..."
```

`model` 和 `PodotionImage4kSk` 都是可选项。新建配置时 `model` 默认为 `gpt-image-2.5`；已有配置缺少 `model` 时仍按 `gpt-image-2` 解释。Skill 安装或更新不会改写 `provider.toml`。`base_url` 固定为 `https://ai.podotion.com/v1`。

### 写入凭据

Windows：

```text
<skill_dir>\bin\podotion-image.cmd config --stdin --force
```

macOS / Linux / WSL：

```text
sh <skill_dir>/bin/podotion-image.sh config --stdin --force
```

stdin 接受 `KEY=value` 形式的赋值，也接受一到两行裸值（默认 SK、可选 4K SK）。追加 4K：

```text
config --stdin --set-4k --force
```

也可以使用 `config --input-file <path> --force`。凭据不会进入命令行参数。

写入凭据后先运行非计费 `doctor`，再重启 Codex 并新建任务。

### 配置默认模型

```text
config set-model gpt-image-3
config --check
```

`set-model` 会保留已有 SK，只更新 `provider.toml` 的 `model`。自然语言行为：

- “以后默认用 `gpt-image-3`” → 运行 `config set-model gpt-image-3`。
- “这次用 `gpt-image-2` 生成” → 该次命令追加 `--model gpt-image-2`，不持久化。
- “用默认模型” → 省略 `--model`。

## 使用

安装并重启后直接描述任务。Skill 会在每个新任务的首次图片操作前执行一次本地预检：

```text
config --check
status
```

```text
生成一张 2560x1440 的绿色山谷头图，保存到 assets/generated。
```

```text
生成一张 4K、16:9 的产品海报，然后把上一张图的背景改成白色。
```

```text
以后默认用 gpt-image-3
```

```text
这次用 gpt-image-2 生成一张 3:2 的封面，下次继续用默认模型。
```

相对输出目录以活动项目 workspace 为基准；未指定时使用 `<workspace>/PodotionImageOutput`。存在多个合理路径或编辑源时，Skill 会在计费请求前询问。

同一任务复用稳定的 `state_scope`，每个独立图片操作使用新的 `request_key`。断连后使用同一组标识运行 `request-status`，不要换 key 重发。只有用户明确承认请求可能已计费时，才运行 `request-abandon --acknowledge-possible-charge`。

## 分辨率与 SK 路由

当前 Skill 对所有模型发送相同的尺寸规则，不根据模型名推断能力：

- 最长边不超过 3840 像素。
- 宽和高都是 16 的倍数。
- 长边与短边之比不超过 3:1。
- 总像素不少于 655,360 且不超过 8,294,400。

用户只给出“4K + 比例”时，使用以下固定映射：

| 比例 | 4K 档尺寸 |
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

| 用户意图或最终尺寸 | 请求形式 | SK |
| --- | --- | --- |
| 未指定尺寸 | `auto` | `PodotionImageSk` |
| 1K、2K 或合法中间尺寸 | 档位或精确尺寸 | `PodotionImageSk` |
| 明确 4K 档 | `4k` 加比例 | `PodotionImage4kSk` |
| 4K 档解析出的规范尺寸 | 精确尺寸 | `PodotionImage4kSk` |
| 总像素达到 8,294,400 | 精确尺寸 | `PodotionImage4kSk` |

例如 `2560x1440` 使用默认 SK，`3840x2160` 使用 4K SK。

## 命令

```text
podotion-image generate
podotion-image edit
podotion-image doctor [--image-probe]
podotion-image sizes
podotion-image config --check
podotion-image config --stdin --force
podotion-image config set-model <name>
podotion-image status
podotion-image update [--dry-run]
podotion-image uninstall --yes
podotion-image uninstall-legacy-plugin --yes
podotion-image request-status
podotion-image request-abandon
```

生成示例：

```text
generate --prompt "<prompt>" --request-key <key> [--model <name>] [--size <tier-or-WIDTHxHEIGHT>] [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> [--force-new]
```

编辑示例：

```text
edit --prompt-file - (--last | --image <path> ...) --request-key <key> [--model <name>] [--size <tier-or-WIDTHxHEIGHT>] [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> [--force-new]
```

成功结果包含 `ok`、`operation`、`model`、`credential_profile`、`request`、`images[]`、`warnings` 和 `state_path`。失败结果写入 stderr，只包含脱敏后的错误信息。

## 状态、更新和卸载

更新固定下载：

```text
https://codeload.github.com/scp-planet/podotion-image/zip/refs/heads/main
```

不接受自定义来源、分支或路径。候选 ZIP 会校验 Skill 名称、必需文件、路径安全和内容摘要，然后在 `$CODEX_HOME` 同卷事务目录中替换安装目录；失败时恢复旧版本。`--dry-run` 只下载和校验。

`uninstall-legacy-plugin --yes` 用于从旧 Plugin 迁移。它只删除旧安装器创建的精确 Marketplace 条目和 `~/plugins/podotion-image` source；同名但来源不同的条目会被拒绝。旧 Plugin 不存在时安全跳过。凭据、新 Skill、工作区图片和请求状态不在清理范围内。

`uninstall --yes` 只移除 `$CODEX_HOME/skills/podotion-image`。凭据、工作区图片和请求状态一律保留。更新、迁移或卸载后必须重启 Codex 并新建任务。

## 项目结构

```text
cmd/podotion-image/          CLI 入口
internal/podotion/           生成、编辑、配置、状态、诊断和生命周期实现
skills/podotion-image/
  SKILL.md
  agents/openai.yaml
  runtime.json
  bin/
    podotion-image.cmd       Windows 启动器
    podotion-image.ps1       Windows 下载与校验逻辑
    podotion-image.sh        macOS/Linux/WSL 启动器
.github/workflows/           CI 与六平台 Release 构建
```

## 开发与测试

要求 Go 1.23+：

```powershell
gofmt -w .
go vet ./...
go test ./...
go build ./cmd/podotion-image
```

测试覆盖：

- 尺寸、4K 映射和双 SK 路由。
- 旧配置兼容、模型优先级、`set-model` 和凭据解析。
- 生成、编辑 multipart、Base64、data URL、URL 下载、响应包装、错误脱敏和超时。
- 请求指纹、幂等复用、模型切换、状态恢复和放弃。
- GitHub ZIP 更新、更新失败保留旧版本、卸载保留凭据、旧 Plugin 迁移。
- CLI 黑盒命令。
- 首次下载、SHA-256 校验失败、缓存命中，以及无 Python 环境运行。

## 发布

`.github/workflows/release.yml` 在 `v*` 标签上构建：

- `windows/amd64`
- `windows/arm64`
- `darwin/amd64`
- `darwin/arm64`
- `linux/amd64`
- `linux/arm64`

每个二进制以 `CGO_ENABLED=0` 构建，并发布对应 `.sha256` 文件。发布新版本时，需要同步更新 `skills/podotion-image/runtime.json` 中的 `version` 和 `tag`，使其指向已发布的 GitHub Release。