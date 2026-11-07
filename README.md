# Podotion Image v2.4.2

All image operations default to `gpt-image-2.6`. Updates migrate managed defaults in `provider.toml` while preserving credentials and explicitly selected user models.

Podotion Image 是一个可由 Codex Skills Install 直接安装的独立 Skill。它使用 Go 原生 CLI 调用 Podotion Images API，支持生成、编辑、精确尺寸、请求恢复、双 SK 路由、诊断、更新、旧 Plugin 迁移和卸载。

用户不需要安装 Python、Node、Git 或其他运行时。Skill 首次运行时会从固定 GitHub Releases 下载与当前 OS/架构匹配的静态二进制，校验 SHA-256 后缓存到：

```text
$CODEX_HOME/podotion-image/runtime/<version>/<os>-<arch>/
```

生图模型可配置。全新配置及升级后的托管默认模型统一使用 `gpt-image-2.6`；用户明确指定的默认模型、凭据和请求记录继续保留。

## 主要变化

- **去 Python 化**：正式运行路径不再包含 Python 脚本，也不要求用户安装解释器。
- **原生 CLI**：Windows、macOS、Linux 的 amd64 与 arm64 均提供 `CGO_ENABLED=0` 静态二进制。
- **模型可配置**：支持 `provider.toml` 的 `model` 字段、单次 `--model` 覆盖和 `config set-model`；全新配置默认 `gpt-image-2.6`。
- **原生更新**：`update` 直接通过 HTTPS 下载 GitHub ZIP，不再依赖 Git。
- **轻量 Skill**：Skill 目录只保留 `SKILL.md`、`agents/openai.yaml`、`runtime.json` 和 `bin/` 启动器。
- **上传修复**：每个 multipart 图片部分按真实内容设置 MIME 和后缀。
- **六种输入格式**：JPEG、PNG、静态 WebP 原字节上传；GIF 首帧、BMP、TIFF 首页转换为 PNG。
- **并发批次**：最多 10 个独立作业并发，提示词和输入先冻结，完成一项立即输出 JSONL 事件。
- **可选输出格式**：默认 PNG；单次参数或批次作业可指定 JPG/JPEG、WebP、GIF，GIF 动画原字节保存。
- **GIF 兼容**：无参考图或带静态参考图都可以请求 GIF 输出；GIF 参考图只取逻辑画布首帧并转为 PNG，不保留动画。

## 功能与取舍

- `auto`、1K、2K 和未达到 4K 标准的精确尺寸使用 `PodotionImageSk`。
- 显式 4K 档、规范 4K 档尺寸或总像素达到 8,294,400 时使用 `PodotionImage4kSk`。
- 旧单 SK 配置继续支持非 4K 请求；4K SK 缺失时在联网和写请求状态前失败，不回退默认 SK。
- 升级自动将托管默认模型迁移为 `gpt-image-2.6`，仅调整模型字段；凭据、图片及请求记录保留。
- 模型优先级固定为：`--model` > 用户指定的默认模型（`model_policy = "user"`）> 托管默认 `gpt-image-2.6`（`model_policy = "default"`）。
- 不根据模型名自动推断能力；尺寸、4K 和双 SK 规则保持不变。未知模型由上游报错。
- 用户给出的合法精确像素尺寸会原样发送，不映射到最近档位。
- 每次上游请求固定 `n=1`，从标准 `data[]` 或明确兼容的 `output[]`、`response.output[]` 中只接受一张最终结果；每个图片操作最多发送一次 POST，不自动重试，请求超时固定为 600 秒。
- 只使用同步 Images API：生成调用 `POST /v1/images/generations`，编辑调用 `POST /v1/images/edits`；不调用 `/async`，不发送 `stream=true`，不轮询异步任务。
- 多图通过 `batch` 执行，每批 1-10 个独立作业，默认全部并发，可用 `--concurrency 1-10` 限制并发数；单项失败不阻断其他独立作业。
- 图片保存在用户工作区，并在回复正文中提供 Markdown 预览和绝对文件链接。
- `doctor` 只做配置和连通性检查，不计费；`doctor --image-probe` 会真实生图并可能计费。

Skill 不提供 MCP `image`、`resource_link`、`resources/list`、`resources/read`、`publish_existing_image` 或 Codex Outputs 面板登记。图片正文预览和本地文件链接仍然保留。

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
model = "gpt-image-2.6"
model_policy = "default"
PodotionImageSk = "sk-..."
PodotionImage4kSk = "sk-..."
```

`model`、`model_policy` 和 `PodotionImage4kSk` 都是可选项。新建配置及托管默认值使用 `gpt-image-2.6`，生成、编辑、GIF 和批次遵循相同规则。`base_url` 固定为 `https://ai.podotion.com/v1`。

`model_policy = "default"` 表示随 Skill 更新的托管默认；`"user"` 表示用户明确选择，升级保留。`config set-model <name>` 自动标记为用户选择，单次 `--model` 不修改配置。更新凭据和追加 4K SK 也保留该标记。

旧版没有记录模型来源，无法可靠区分历史默认值和同名的手工选择。无来源标记且模型缺失或为 `gpt-image-2`、`gpt-image-2.5`、`gpt-image-2.6` 时迁移为托管默认；其他自定义模型保留。如果用户明确要保留旧默认同名模型，先运行一次 `config set-model <name>` 固定选择。可用 `config migrate-default --dry-run` 只读预览迁移，`config --check` 只读报告有效模型及来源。

v2.4.2 原生升级器会在更新事务中执行模型迁移；旧版升级器只替换 Skill 时，新版 Skill 会在升级后的补充步骤或下个任务首次生图预检中自动调用 `config migrate-default`，无需用户手工改配置。失败升级不会迁移配置；迁移失败则恢复旧安装。

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

`set-model` 会保留已有 SK，更新 `provider.toml` 的 `model` 并设置 `model_policy = "user"`。自然语言行为：

- “以后默认用 `gpt-image-3`” → 运行 `config set-model gpt-image-3`。
- “这次用 `gpt-image-2` 生成” → 该次命令追加 `--model gpt-image-2`，不持久化。
- “用默认模型” → 省略 `--model`。

## 使用

安装并重启后直接描述任务。Skill 会在每个新任务的首次图片操作前执行一次本地预检：

```text
config migrate-default
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
podotion-image batch --input-file <manifest.json> --output-dir <absolute_dir> --state-scope <scope> [--concurrency N]
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
generate --prompt "<prompt>" --request-key <key> [--model <name>] [--output-format png|jpg|jpeg|webp|gif] [--size <tier-or-WIDTHxHEIGHT>] [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> [--force-new]
```

编辑示例：

```text
edit --prompt-file - (--last | --image <path> ...) --request-key <key> [--model <name>] [--output-format png|jpg|jpeg|webp|gif] [--size <tier-or-WIDTHxHEIGHT>] [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> [--force-new]
```

成功结果包含 `ok`、`operation`、`model`、`output_format`、`credential_profile`、`request`、`images[]`、`warnings` 和 `state_path`。单图失败结果写入 stderr，只包含脱敏后的错误信息。批次的逐项结果和最终汇总写入 stdout，每行一个 JSON；预检失败写入 stderr。

### 输出格式与 GIF 兼容

不指定格式时继续请求 PNG，不增加配置字段、不修改用户默认模型。`--output-format` 不区分大小写，接受 `png`、`jpg`、`jpeg`、`webp`、`gif`；`jpg` 发送为 API 的 `jpeg` 并保存为 `.jpg`。批次每项可独立使用 `output_format`，支持混合格式并发。

自然语言“生成 JPG 图片”使用 `jpeg`，“生成 GIF 动图”使用 `gif` 并描述运动与循环。模型与 Podotion 是否支持该格式由上游决定，客户端不推断模型能力或偷偷换模型。官方 API 还要求所选模型具有 GIF preview 访问权限；模型列表包含某模型不代表已开通其全部能力。GIF 的帧数、时长使用上游默认值，不传流式、部分图片、压缩或提示词重写参数。

GIF 输出同时适用于生成和编辑请求，最终能力由上游模型和凭据权限决定。静态 PNG、JPEG、WebP、BMP、TIFF 参考图可以请求 GIF 输出；GIF 输入只解码逻辑画布首帧并转为 PNG 后上传，因此不会伪装成动画编辑，也不会保留源 GIF 的帧序列。上游拒绝 GIF 时保留真实的脱敏错误，不把鉴权、额度、超时或模型能力错误改写成本地限制。

官方依据（核对日期 2026-11-05）：

```text
https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output
https://developers.openai.com/api/reference/resources/images/methods/generate
https://developers.openai.com/api/reference/resources/images/methods/edit
```

输出按真实内容确定 MIME 和扩展名，GIF 的帧、帧延迟、透明度和循环信息原样保留，绝不走输入首帧转换。顶层与 `request.output_format` 是请求格式，`images[].output_format` 是实际格式；不一致时保留实际文件并返回 `image_format_mismatch` 提示，不改后缀伪装，不自动重试。JPEG、GIF、WebP 同样报告可解析尺寸。旧请求与旧批次快照缺少格式时继续视作 PNG，保留旧 PNG 指纹；不同格式不复用结果。

### 编辑输入格式

| 真实格式 | 上传格式与处理 |
| --- | --- |
| JPEG / PNG / 静态 WebP | 完整解码验证后保留原字节，使用 `image/jpeg`、`image/png`、`image/webp` |
| GIF | 按逻辑画布提取首帧并转 PNG，保留透明度，返回 `input_first_frame` 提示 |
| BMP | 解码后转 PNG |
| TIFF | 首页转 PNG，保留可解码的透明度，返回 `input_first_page` 提示 |

按内容识别，忽略错误的扩展名。每次最多 5 张，每张转换前后均不超过 20 MB，解码不超过 5,000 万像素。HEIC、AVIF、动画 WebP、损坏文件与解码器不支持的变体会在提交前报错。输出图片识别保持原有协议。解码器使用固定版本的纯 Go `golang.org/x/image`，用户无需安装转换工具。

### 并发批次与恢复

```json
{
  "schema": 1,
  "batch_id": "cats-001",
  "jobs": [
    {"job_id": "a", "operation": "generate", "prompt": "橘猫坐在阳台，写实照片", "size": "2k", "ratio": "3:2"},
    {"job_id": "b", "operation": "generate", "prompt": "橘猫坐在阳台，写实照片", "model": "gpt-image-2.6", "size": "2k", "ratio": "3:2"},
    {"job_id": "edit-a", "operation": "edit", "prompt": "将背景换成纯白", "images": ["input.jpg", "reference.png"]}
  ]
}
```

`batch_id`、`job_id` 使用 1-128 位字母、数字、点、下划线或短横线，首位为字母或数字。`job_id` 在批内唯一。编辑路径相对清单所在目录解析。先确定全部提示词，再写一次清单并运行 `batch`。同提示词的不同作业是独立变体；需要前一张结果的编辑应放到下一步。

命令开始前会冻结默认模型、输出格式、尺寸、提示词和输入快照，存入输出目录的 `.state/<scope>/batches/`，其中不含凭据。恢复时保持原清单、`batch_id`、`job_id`、输出目录和 scope。成功项复用，失败或未知项不会重发；独立项继续完成。变更同一批次的清单会被拒绝。快照包含提示词和参考图片，请按工作区资料保管。

`job_completed` 行含 `index`、`job_id`、`result`，完成即输出；最后的 `batch_completed` 含按原顺序排列的 `jobs`、`succeeded`、`failed`。任一项失败时退出码非零，成功图片仍保留。父级 `last.json` 按清单顺序保存所有成功图片；存在多张时，`edit --last` 会要求选图。

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
- 各输入格式的真实解码、转换、透明度和 multipart 每部分 MIME / 文件名 / 字节 / 顺序，以及外层 boundary。
- 无效输入零 POST、10 作业同步屏障并发、失败隔离、唯一文件名、请求键锁、冻结快照与安全恢复。
- 多格式请求参数、GIF 动画字节和时序保留、真实 MIME 与扩展名、混合输出并发与恢复、格式指纹隔离、旧 PNG 状态兼容、GIF 生成与编辑兼容、GIF 参考图首帧转换。

CI 在 Windows、macOS、Linux 执行测试，Linux 额外执行 `go test -race ./...`，并继续交叉构建六平台静态产物。

### 本机真实接口回归（不入 Git）

专用 Go 工具、二进制、合成样例、私有 `provider.toml`、执行预算和 JSON / HTML 报告固定放在：

```text
C:\Users\scp-planet\.codex\podotion-image-regression\
```

运行入口（Windows）：

```powershell
& 'C:\Users\scp-planet\.codex\podotion-image-regression\regression.exe' run --binary '<repo>\dist\podotion-image-windows-amd64.exe'
```

默认模型 `gpt-image-2.6`，可用 `--model` 覆盖。先将独立测试 SK 写入工具目录的专用 `provider.toml`；不读取日常凭据。每次新运行建立隔离 workspace 和 `CODEX_HOME`。完整回归最多 5 个真实 POST：两张并发生图、JPEG 编辑、JPEG＋PNG＋WebP 编辑、GIF＋BMP＋TIFF 编辑。`resume --run <目录>` 沿用预算和请求身份；未知结果不换身份重试。工具检查文件可解码、数量及状态，不自动评判画面内容，也不会重发此前报告的证件照请求。

发布后运行 `regression.exe upgrade`，在隔离环境实际安装 v2.2.0、执行旧启动器 `update --dry-run` 和 `update`，再检查新启动器下载、版本及缓存命中，并比较配置、模型及请求记录。升级验证不额外生图。后续可直接让 agent“运行 Podotion 本地回归”。

输出格式回归入口为同目录下的 `regression.exe formats --binary '<候选二进制>' --model gpt-image-2.6`，同样最多 5 次请求：PNG、JPG、WebP、GIF 四项混合格式并发生成，以及一次混合参考图的 JPG 编辑。服务端返回错误格式会明确记为验证未通过，不伪装成功。`formats --run <原运行目录>` 使用冻结的二进制、配置和原请求身份恢复，未知或失败请求不换标识重发。

## 发布

`.github/workflows/release.yml` 在 `v*` 标签上构建：

- `windows/amd64`
- `windows/arm64`
- `darwin/amd64`
- `darwin/arm64`
- `linux/amd64`
- `linux/arm64`

每个二进制以 `CGO_ENABLED=0` 构建，并发布对应 `.sha256` 文件。版本常量、运行时清单和 Skill 简介统一为 v2.4.2，保留旧版发布。v2.3.1 的 Windows 并发状态隔离修复继续保留。

发布顺序：先将候选提交推送为 `v2.4.2` 标签，等待六平台二进制和校验资源发布并验证可下载，再让 `main` 指向该提交。不要在资源发布前把新 `runtime.json` 推到 `main`。随后验证旧启动器升级和新版 `config migrate-default` 补迁移，确认历史默认值更新、用户选择保留、凭据和请求记录保留。
