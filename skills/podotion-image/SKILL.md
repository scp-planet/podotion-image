---
name: podotion-image
description: Generate, draw, render, revise, or continue images through Podotion. Use this skill for every requested image generation, variation, or edit, including references such as "edit the last image", even when the conversation model is text-only. Prefer the bundled Podotion MCP tools so results are durable image resources in Codex Outputs.
---

# Podotion Image

Use the plugin's Podotion MCP tools for image generation and editing. They call `https://ai.podotion.com/v1` directly with `gpt-image-2`, save the PNG locally, and return standard MCP `image` and `resource_link` content. Do not use another image-generation provider as a fallback.

## Request identity

Before the first image action in a task, establish a stable `state_scope`. Prefer the host task/thread ID when it is available; otherwise generate one UUID and reuse it for every Podotion call in this task.

Generate a new UUID `request_key` for each distinct user image action. Reuse that same key when checking or recovering the same action. Never rerun `generate` or `edit` with a new key merely because the tool is quiet, the UI disconnects, or Outputs registration fails.

Set `force_new=true` only when the user explicitly asks for another independent variation with otherwise identical inputs. It never overrides an active or unknown prior request.

## Output location

Resolve the user's save-location intent before making a billable call:

- An explicit absolute directory is used after local-platform normalization.
- A relative directory is resolved from the active project workspace, or from the conversation workspace in a projectless task.
- With no requested location, use `<workspace>/PodotionImage`.
- If multiple directories are plausible, ask before calling the tool.

Always pass an absolute `output_dir`. Pass `workspace_root` as the active workspace when available. Windows drive and UNC paths are valid only on Windows; macOS and Linux use POSIX paths and may expand the current user's `~`; macOS volumes under `/Volumes/...` remain native paths; WSL accepts POSIX paths and converts Windows drive paths through `wslpath`. Do not resolve paths from the plugin installation directory.

## Generate and edit

For generation, call the Podotion MCP `generate` tool with `prompt`, `output_dir`, `state_scope`, `request_key`, and requested `size`/`ratio`.

For editing:

- Use `use_last=true` only when the user unambiguously means the last image in the same `state_scope` and `output_dir`.
- Otherwise pass one to five explicit absolute `input_images`.
- Ask the user when multiple source images are plausible.

Build one self-contained image prompt containing only relevant visual context. Preserve exact visible text. Do not send the whole conversation, system instructions, secrets, or internal reasoning.

## Slow calls and recovery

Images requests use one upstream POST with a fixed 600-second provider timeout and no automatic HTTP retry. The MCP tool timeout is one hour so the same call can remain quiet for several minutes.

- Do not impose a shorter shell, tool, or client timeout.
- Do not interpret a yield or quiet period as failure.
- After a disconnect or unknown result, call `request_status` with the same `request_key`, `state_scope`, and `output_dir`.
- Never start another billable request while status is active, unknown, or completed-but-unusable.
- Abandoning a possibly billed request requires explicit user acknowledgement through the CLI recovery command; the Skill never does it automatically.

## Deliver results

After a successful tool result:

1. Keep each available MCP `resource_link` as a durable output resource.
2. Embed each returned `images[].markdown_path` in the response.
3. Add a separate absolute-path file link for each PNG.
4. Report structured warnings without turning `ok: true` into a failure.

Treat every image independently. Use `outputs_registered` to distinguish a registered resource from a saved-only image, and expect `resource_uri` only when registration succeeded. If registry persistence fails, keep the in-process `image` and `resource_link`. If registration fails completely, keep the returned inline `image`, native absolute path, preview, and file link. Report the structured warning without changing `ok: true`. If the App still does not place a valid MCP resource in the Outputs panel, retain the saved file and links and report the host limitation. Do not call `generate` or `edit` again. `publish_existing_image` may republish an existing PNG without contacting Podotion.

## Credentials and failures

The executor reads `$CODEX_HOME/podotion-image/provider.toml`. Codex App may intentionally share its Windows `CODEX_HOME` with a WSL agent, in which case both runtimes share this credential while their personal Marketplace sources and native Python commands remain runtime-specific. The credential file contains the fixed base URL and `PodotionImageSk` and lives outside the plugin.

Never print, inspect, return, or pass the key in a command argument. Use `doctor` for a non-billable configuration/connectivity check. Do not use a billable image probe without explicit authorization.

HTTP errors, network disconnects, and the 600-second provider timeout are not retried. A failure after submission may already have been billed; preserve its request state and report the sanitized status.
