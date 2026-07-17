# Podotion Image

Podotion Image 是一个面向 Codex Desktop 和 Codex CLI 的跨平台 Plugin。它通过独立的 MCP 工具直接请求 Podotion Images API，固定使用 `gpt-image-2` 生成或编辑图片，并将原始 PNG 保存到当前项目或任务工作区。

Plugin 的正式标识为 `podotion-image`；Python 包名为 `podotion_image`。

## 功能与运行保证

- 生成请求使用 `POST /v1/images/generations`，编辑请求使用 `POST /v1/images/edits`。
- 每个用户图片操作最多发送一次上游 POST，不自动重试，也不回退到 Responses API 或其他生图服务。
- Images 请求超时固定为 600 秒；MCP 工具超时为 3600 秒。生图期间数分钟无输出属于正常情况。
- 稳定的 `request_key` 和本地请求记录用于查询断连或结果未知的请求，避免因重发而重复计费。
- 请求记录不保存密钥、提示词、Base64 图像或 multipart 请求体。

## 系统要求

- Codex Desktop 或 Codex CLI，并支持个人 Plugin Marketplace。
- Python 3.11 或更高版本。
- Git。
- 可用的 PodotionImageSk，其所属分组已开启图片生成，并能路由到 `gpt-image-2` 图片账号。

Windows、WSL、macOS 和 Linux 都使用安装时当前运行时的原生 Python。不要在 Windows 中启动 WSL Python，也不要在 WSL 中启动 Windows Python。

## 从 GitHub 安装或更新

将下方整段提示词发给 Codex。它同时适用于首次安装和更新；安装器会以事务方式替换个人 Plugin，并在失败时回滚。Codex Desktop 任务须先开启完全访问权限；Codex CLI 则须使用对当前用户目录具有等效写权限的运行账户。

```text
请从 https://github.com/scp-planet/podotion-image.git 安装或更新 podotion-image Plugin。我的 PodotionImageSk 是 {{PodotionImageSk}}。

前置条件：开始前确认当前运行时可访问 GitHub，且具备对以下当前用户目录的写权限：`~/plugins/podotion-image`、`~/.agents/plugins/marketplace.json` 和 `$CODEX_HOME`。在 Codex Desktop 中，这要求任务已开启完全访问权限；若当前仍是仅工作区写入的沙箱，先要求我重新以完全访问权限启动任务并停止，不得开始 clone。不得用 `--home`、`--codex-home`、`--no-codex` 或工作区替代这些目标目录。

请在当前 Codex 运行时内执行以下步骤：
1. 创建权限受限的随机临时目录，使用当前运行时的原生 Git 执行：git clone --depth 1 https://github.com/scp-planet/podotion-image.git <clone-dir>。
2. 仅在原生 Windows 中，且首次 clone 错误同时包含 schannel、AcquireCredentialsHandle failed 和 SEC_E_NO_CREDENTIALS 时，新建另一个权限受限的随机临时目录，对同一 GitHub URL 只重试一次：git -c http.sslBackend=openssl clone --depth 1 https://github.com/scp-planet/podotion-image.git <new-clone-dir>。这是唯一允许的下载降级；不得执行 git config --global 或 --system，不得设置 http.sslVerify=false，不得改用代理、镜像、归档下载、附件或 WSL。错误类型不同或该次重试仍失败时立即停止。
3. clone 成功后确认退出状态为 0、origin 严格等于上述 GitHub 地址且 git rev-parse --is-shallow-repository 返回 true。仅当这些检查通过时，无法读取全局 excludesfile 的警告可以忽略。校验 .codex-plugin/plugin.json、.mcp.json、skills/podotion-image/SKILL.md、mcp/server.py、scripts/install.py 和生图执行器都存在。
4. 先运行 scripts/install.py --dry-run 并检查计划，再在同一完全访问权限范围内执行正式安装。正式安装必须保留 `codex plugin add podotion-image@personal` 注册步骤；不得跳过注册或改写 Marketplace 文件。通过标准输入将上述密钥传给安装后的 configure_direct.py --stdin --force；不得把密钥放入命令行参数、环境变量、日志、仓库或回复内容。
5. 运行不计费的 doctor 检查，不得运行 --image-probe。成功后删除本次创建的所有临时目录，告知我安装目录、配置路径和检查结果，但不得显示密钥。

如果无法满足前置权限、远程仓库在上述唯一一次受限重试后仍不可访问，或任一来源、文件、安装计划或正式安装失败，立即停止，不得改用未经校验的其他来源或替代安装路径。
```

安装或更新完成后，新建一个 Codex 任务，使 Plugin Skill 和 MCP server 按新配置加载。

### Windows Schannel 克隆故障

部分 Windows Codex 进程调用 Git for Windows 时，Schannel 可能在 TLS 初始化阶段返回 `SEC_E_NO_CREDENTIALS`。这不是 GitHub 仓库凭据错误，也不能通过关闭 TLS 校验解决。

安装提示词只允许对此特定错误使用单命令配置：

```powershell
git -c http.sslBackend=openssl clone --depth 1 https://github.com/scp-planet/podotion-image.git <new-clone-dir>
```

该参数不会修改用户级或系统级 Git 配置。若 OpenSSL 重试失败，应保留脱敏错误并停止安装；不要永久切换 Git 后端，也不要绕过证书验证。

### 手动运行安装器

如需本地开发或排查，可在克隆后的仓库根目录手动执行。

macOS、Linux 或 WSL：

```bash
python3 scripts/install.py --dry-run
python3 scripts/install.py
```

Windows PowerShell：

```powershell
py -3 scripts\install.py --dry-run
py -3 scripts\install.py
```

安装器会把 Plugin 源码安装到当前运行时的 `~/plugins/podotion-image`，维护 `~/.agents/plugins/marketplace.json`，并注册 `podotion-image@personal`。

手动配置凭据和运行不计费检查：

```bash
python3 ~/plugins/podotion-image/skills/podotion-image/scripts/configure_direct.py --force
python3 ~/plugins/podotion-image/skills/podotion-image/scripts/podotion_image.py doctor
```

Windows PowerShell：

```powershell
py -3 "$env:USERPROFILE\plugins\podotion-image\skills\podotion-image\scripts\configure_direct.py" --force
py -3 "$env:USERPROFILE\plugins\podotion-image\skills\podotion-image\scripts\podotion_image.py" doctor
```

### 安装目录的职责

- 随机临时 clone 仅用于从 GitHub 下载并校验一次安装输入。正式安装完成后删除它，可避免残留密钥操作上下文、过期 checkout 和重复源码。
- `~/plugins/podotion-image` 是持久的个人 Marketplace 源。安装器把校验后的 Plugin 复制到这里，后续可从这里重新注册。
- `$CODEX_HOME/plugins/cache/personal/podotion-image/<version>` 是 Codex 管理的已安装副本。Codex App 正常从这个副本加载 Skill 和 MCP；它不是随机 Git 临时目录。

不要手工编辑 Plugin cache。若 cache 被手动清空，可从持久 Marketplace 源直接重新注册：

```bash
codex plugin add podotion-image@personal
```

也可以重新执行上方 GitHub 安装提示词，从新的临时 clone 完整安装。不要在 `~/plugins/podotion-image` 内直接运行安装器，因为该目录已经是安装目标。恢复后重启 Codex App 并新建任务，让 Skill 和 MCP server 重新加载。

## 跨平台目录规则

`CODEX_HOME` 始终优先。未显式设置时，原生 Windows 回退到 `%USERPROFILE%\.codex`，WSL、macOS 和 Linux 回退到 `$HOME/.codex`。

| 运行时 | Plugin 与个人 Marketplace | 默认 Codex 配置 | 路径要求 |
| --- | --- | --- | --- |
| Windows | `%USERPROFILE%\plugins\podotion-image` | `%USERPROFILE%\.codex` | 支持盘符绝对路径和 UNC；不接受 `\\wsl$` 或 `\\wsl.localhost` 作为 `CODEX_HOME` |
| WSL | `$HOME/plugins/podotion-image` | `$HOME/.codex` 或共享的 `/mnt/<drive>/Users/<name>/.codex` | 接受 POSIX 路径；Windows 路径需使用挂载形式或由 `wslpath` 转换 |
| macOS | `$HOME/plugins/podotion-image` | `$HOME/.codex` | 使用 POSIX 路径；支持 `~/...` 和 `/Volumes/...` |
| Linux | `$HOME/plugins/podotion-image` | `$HOME/.codex` | 使用 POSIX 路径和当前用户的 `~/...` |

Codex App 可以在 WSL 内运行 agent，同时设置 `CODEX_HOME=/mnt/c/Users/<name>/.codex` 共享 Windows Codex 状态。在此布局中：

- 凭据位于 `$CODEX_HOME/podotion-image/provider.toml`，Windows 和 WSL 共用同一份凭据。
- 安装副本的 `.mcp.json` 显式传递安装时解析出的绝对 `CODEX_HOME`，即使 MCP 子进程未继承 Codex App 的环境，也会读取同一份凭据。
- Plugin 源、个人 Marketplace、原生 Python 和 Outputs 资源登记仍跟随当前运行时的 `HOME`。
- WSL 的平台 marker 位于 `$CODEX_HOME/.podotion-image-runtimes/wsl/`，不与原生 Windows marker 冲突。
- 在 Codex App 中切换 Windows 与 WSL 运行时后，需在新运行时中重新运行一次安装器，然后新建任务。

## 凭据与安全

凭据文件位于 `$CODEX_HOME/podotion-image/provider.toml`，不属于 Plugin 源码、安装目录或 Git 仓库。配置器会尽力将目录权限设为 `0700`、文件权限设为 `0600`。

- 不要在命令行参数、Git 提交、Issue、日志或截图中放入密钥。
- 使用 `configure_direct.py` 的隐藏输入，或在受控流程中使用 `--stdin`。
- `doctor` 只执行配置和连通性检查，不计费。`doctor --image-probe` 会发送真实生图请求，必须获得明确授权。
- 上游请求失败或超时时，请求可能已经计费。先查询原 `request_key` 的状态，不要用新 key 盲目重发。

## 输出目录

Skill 在计费请求前从用户自然语言中解析保存意图：

- 明确的绝对目录经当前平台规范化后使用。
- 明确的相对目录以活动项目 workspace 为基准；无项目任务则以任务 workspace 为基准。
- 未指定位置时使用 `<workspace>/PodotionImage`。
- 有多个合理目录或路径无法可靠解析时，Skill 必须先询问，不发起生图请求。

图片状态存放在输出目录的 `.state/<scope>/` 中，使 `edit --last` 和请求恢复按任务隔离。

## Outputs 与成功降级

MCP server 返回标准 `image` 内容块，并在资源登记成功时返回 `resource_link`。它同时实现 `resources/list` 和 `resources/read`，这是 Codex Outputs 最适合识别的协议表示。

上游生成成功且 PNG 已落盘后，Outputs 后处理不会将结果改为失败：

- 每张图片独立验证、登记和描述路径，一张图的错误不会隐藏其他已保存图片。
- 登记表持久化失败时，当前 MCP 进程保留内存资源和 `resource_link`。
- 资源登记完全不可用时，仍返回已保存 PNG 的内联 `image`、原生绝对路径和结构化 warning；该图不提供 `resource_uri`。
- 路径描述失败时保留原生绝对路径。
- 结构化图片结果使用 `outputs_registered` 表明资源是否已登记；只有登记成功时才提供 `resource_uri`。

Outputs 面板是 Codex 宿主功能。宿主未展示有效 MCP 资源时，Plugin 仍会保留 PNG、正文预览和文件链接，并返回 warning；不会重新请求生图。`publish_existing_image` 可在不联系 Podotion 的情况下重新发布已有 PNG。

## 卸载

先从当前运行时的 Codex 中移除 Plugin：

```bash
codex plugin remove podotion-image@personal
```

然后从当前运行时的 `~/.agents/plugins/marketplace.json` 中删除 `podotion-image` 条目，并删除 `~/plugins/podotion-image`。凭据不会随 Plugin 卸载自动删除；仅在确认不再使用时手动删除 `$CODEX_HOME/podotion-image/provider.toml`。共享 `CODEX_HOME` 时，删除凭据会同时影响 Windows 和 WSL。

## 开发、测试与发布

在仓库根目录运行全部单元测试：

```bash
python3 -m unittest discover -s tests -v
```

执行 Python 编译检查、安装计划预览和 Skill 验证：

```bash
python3 -m compileall -q mcp podotion_image scripts skills/podotion-image/scripts tests
python3 scripts/install.py --dry-run
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/podotion-image
```

构建可分发 ZIP：

```bash
python3 scripts/build_release.py
```

产物写入 `dist/podotion-image-plugin.zip`，ZIP 的唯一顶层目录为 `podotion-image/`。构建器不打包 `.git`、`dist`、Python 缓存、测试缓存或本地生成图片目录。正式源码始终以仓库根目录为唯一真实来源，`dist/` 不应提交到 Git。

发布前至少确认：全部测试通过、Skill 验证通过、安装器在目标运行时的 `--dry-run` 通过、ZIP 不包含缓存或真实凭据。只有在明确授权时才执行计费的端到端生图检查。
