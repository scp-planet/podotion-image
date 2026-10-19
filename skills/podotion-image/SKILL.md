---
name: podotion-image
description: Generate, draw, render, revise, or continue images through Podotion, and configure, diagnose, update, migrate from the legacy Plugin, or uninstall the Podotion Image Skill. Use for every requested Podotion image generation or edit, including references such as "edit the last image", and whenever the user asks to install, configure a default model, or manage this Skill. Do not trigger for text-only explanations about image models.
---

# Podotion Image

Use the bundled native launcher. Do not look for Plugin or MCP tools, and do not require Python, Node, or Git. The launcher runs a checksummed Go binary that calls `https://ai.podotion.com/v1` and saves PNG files in the user's workspace.

## Runtime

Resolve `<skill_dir>` as the directory containing this `SKILL.md`. Keep the active project or conversation workspace as the process working directory.

- On native Windows, run `<skill_dir>\bin\podotion-image.cmd`.
- On macOS, Linux, and WSL, run `sh <skill_dir>/bin/podotion-image.sh`.
- Pass all commands and options through the launcher, for example `generate`, `edit`, `doctor`, `sizes`, `config`, `status`, `update`, `uninstall`, `request-status`, and `request-abandon`.

On first use the launcher downloads the matching native binary from the fixed GitHub Releases location, verifies its SHA-256 checksum, and caches it under `$CODEX_HOME/podotion-image/runtime/<version>/<os>-<arch>/`. Later runs reuse the cache. Do not install packages or runtimes. If the download fails, report the sanitized error and stop.

## First-image preflight

Before the first generate or edit action in every new task, run `config --check` and `status`. Both checks are local and read-only: they must not contact Podotion, print credential values, write files, or create an image request. Run them once per task, not before every image action.

For a multi-image request, resolve every requested image's size and credential profile before the first billable call. If any image needs an unavailable credential, complete credential onboarding before generating any image; do not leave the user with a partially generated batch followed by a configuration restart.

- If the configuration check is missing or invalid, request the default credential as described below and do not invoke an image action.
- If the current action routes to 4K and `credential_profiles.4k` is false, request only the 4K credential. A valid default-only config remains ready for non-4K actions.
- If `legacy_plugin.detected` and `legacy_plugin.safe_to_remove` are both true, tell the user that the old Plugin is still installed and ask whether to migrate it. Ask again on the first image action of every new task until migration succeeds; a refusal applies only to the current task. If the user declines and the needed credential profile is valid, continue the requested image action with the native Skill.
- If a legacy installation is detected but `safe_to_remove` is false, report the sanitized status and do not ask for cleanup confirmation or attempt cleanup.

When a credential prompt and a safe legacy migration prompt are both needed, ask for the required assignment lines and the migration yes/no decision in one response.

## Credentials

Read credentials only through the launcher. They live at `$CODEX_HOME/podotion-image/provider.toml`, falling back to the current platform's `~/.codex/podotion-image/provider.toml`.

- `PodotionImageSk` is required and handles `auto`, 1K, 2K, and non-4K exact sizes.
- `PodotionImage4kSk` handles explicit 4K-tier requests, canonical 4K-tier dimensions, and exact sizes with at least 8,294,400 pixels.

Legacy files containing only `PodotionImageSk` remain valid for non-4K work. A 4K request without `PodotionImage4kSk` must fail before any output directory, request state, or provider request is created. Never fall back to the default key, and do not request a 4K key until the user asks for a 4K-routed image.

Accept credential input in either of these forms:

- An inline `PodotionImageSk=<value>` or `PodotionImage4kSk=<value>` line in the user's current message.
- A UTF-8 text attachment containing the same `KEY=value` line format. Accept at most the two recognized keys and reject unrelated content.

Recommend an attachment when practical. It avoids placing the literal key in the chat prompt or process argv, but its contents still become input to the current Codex session; do not describe it as an out-of-session secret channel. Inline input is also accepted when the user prefers it.

Never repeat a credential in commentary, commands, logs, or the final response. Send an inline assignment block unchanged through process stdin to `config --stdin --force`. When the host exposes an attachment as a local path, pass only that path to `config --input-file <path> --force`; if no path is available, send the attachment content through stdin. To add only 4K to an existing config, add `--set-4k`. If multiple attachments could contain credentials, ask which one to use. Never scan unrelated files.

After any successful credential write, run non-billable `doctor` without `--image-probe`, report one sanitized readiness line, and stop. Tell the user to restart Codex and create a new task before generating images. Do not continue the original image request in the current task after changing credentials.

## Model configuration

The image model is configurable. A newly created `provider.toml` defaults to `gpt-image-2.5`; existing configuration, including legacy files without a `model` field, is left unchanged by installation or update.

- The persisted default lives in `provider.toml` as the optional `model = "<name>"` field.
- Model precedence is fixed: a per-call `--model <name>` overrides `provider.toml`, which overrides the new-install default `gpt-image-2.5`. Existing files without `model` continue to resolve to `gpt-image-2`.
- Never rewrite `provider.toml` during Skill installation or update; only explicit `config`, `config set-model`, or `config --set-4k` actions may change it.
- Never infer capabilities, pricing, or size limits from the model name. Keep the existing size, 4K, and dual-SK rules. Send unknown model names to the upstream API and report its sanitized error.

Handle natural-language model intent as follows:

- "以后默认用 <model>" / "set <model> as the default": run `config set-model <model>` and report the saved default. This preserves credentials and does not require a restart. Continue a pending image request only if the user still expects it in the same task.
- "这次用 <model> 生成" / "use <model> for this one": add `--model <model>` to exactly that `generate` or `edit` invocation. Do not persist it.
- "用默认模型" / "use the default model": omit `--model`; the configured default is used.
- When the user asks which model is active, run `config --check` and report only the model name and credential-profile availability.

Examples:

```text
以后默认用 gpt-image-3
这次用 gpt-image-2 生成一张 16:9 的产品海报
改回默认模型
```

## Generate and edit

Before composing the prompt, read `sizes` when the user asks for a tier or ratio and the mapping is not already known. The CLI accepts `--size auto`, `1k`, `2k`, `4k`, or an exact `WIDTHxHEIGHT`, plus `--ratio` only when `--size` is a tier.

```text
generate --prompt "<prompt>" --request-key <key> [--model <name>] [--size <tier-or-WIDTHxHEIGHT>] [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> [--force-new]
```

```text
edit --prompt-file - (--last | --image <path> ...) --request-key <key> [--model <name>] [--size <tier-or-WIDTHxHEIGHT>] [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> [--force-new]
```

Each image action uses one upstream POST with a fixed 600-second timeout and no automatic HTTP retry. Allow the command to remain quiet for several minutes.

Every invocation requests exactly one image with `n=1` and accepts exactly one final image from one known synchronous response container. Prefer the standard Images API `data[]`; when it is absent, null, or empty, accept the exact Responses-compatible `output[]` or `response.output[]` shape containing `type=image_generation_call` with absent status or `status=completed`. Never merge containers, recursively scan arbitrary wrappers, or consume partial-image fields. Never ask one invocation to return multiple images. If the selected container has zero or multiple final images, treat it as a completed-but-unusable protocol error and do not retry.

When the user requests multiple images:

- Require an explicit count from 1 through 10; ask for the count when it is missing, and ask the user to split requests above 10 into separate tasks.
- Resolve all prompts, sizes, models, credentials, and output locations before the first billable call.
- Run one complete image action at a time. Wait until its single image is decoded, atomically saved, and durably recorded before starting the next action. Never launch these actions in parallel.
- Immediately present each successfully saved image and its absolute local file link before starting the next image action. Do not wait for the whole batch to finish before returning earlier results.
- Reuse the task's `state_scope`, but assign each action a new `request_key`. Add `--force-new` to the second and later actions when their effective prompts and other inputs are identical.
- Stop immediately after any failure, disconnect, unknown result, or completed-but-unusable result. Preserve earlier successful images, report the failed one-based position and sanitized status, and do not start the remaining actions.

## Recovery

After a disconnect or unknown result, run `request-status` with the same `request_key`, `state_scope`, and `output_dir`. Never start another billable request, including the next action in a multi-image sequence, while status is active, unknown, or completed-but-unusable.

Use `request-abandon --acknowledge-possible-charge` only after the user explicitly acknowledges that the uncertain request may already have been billed.

## Deliver results

Each successful result has exactly one item in `images[]`. For every successful image, including earlier images from a stopped multi-image sequence:

1. Embed `images[].markdown_path` as a Markdown image in the final response.
2. Add a separate absolute local file link to `images[].path`.
3. Report structured warnings without turning `ok: true` into a failure.

This Skill does not register MCP `resource_link` entries or publish to the Codex Outputs panel. Never repeat a provider request to improve presentation.

## Manage the Skill

Run lifecycle commands only when the user explicitly requests them:

- Status: `status`.
- Update: run `update --dry-run`, then `update`.
- Legacy Plugin migration: after the user accepts the current task's migration prompt, run non-billable `doctor` without `--image-probe`. Only when that current doctor succeeds, run `uninstall-legacy-plugin --yes`. The command removes only the owned `podotion-image@personal` registration and exact legacy source, and preserves credentials, the new Skill, images, and request state.
- Uninstall: explain that credentials and generated images are preserved, obtain explicit confirmation, then run `uninstall --yes`.

Updates download the fixed GitHub repository ZIP over HTTPS and replace the installed Skill transactionally; they do not require Git, Python, or Node. After update, migration, uninstall, or completed credential onboarding, stop; tell the user to restart Codex and create a new task. Do not continue using replaced or removed Skill code.

## Failures

HTTP errors, network disconnects, and provider timeouts are not retried. A failure after submission may already have been billed; preserve its request state and report one concise, sanitized reason. Do not reproduce raw upstream bodies, stack traces, nested diagnostic payloads, credentials, authorization headers, or full provider configuration in the user-facing response.