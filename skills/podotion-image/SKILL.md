---
name: podotion-image
description: Generate, draw, render, revise, or continue images through Podotion, and configure, diagnose, update, migrate from the legacy Plugin, or uninstall the Podotion Image Skill. Use for every requested Podotion image generation or edit, including references such as "edit the last image", and whenever the user asks to install or manage this Skill. Do not trigger for text-only explanations about image models.
---

# Podotion Image

Use the bundled standalone Python scripts. Do not look for Plugin or MCP tools. The scripts use Python 3.11+ standard library only, call `https://ai.podotion.com/v1` with `gpt-image-2`, and save PNG files in the user's workspace.

## Runtime

Resolve `<skill_dir>` as the directory containing this `SKILL.md`. Keep the active project or conversation workspace as the process working directory.

- On native Windows, use `py -3`.
- On macOS, Linux, and WSL, use `python3`.
- Use `<skill_dir>/scripts/podotion_image.py` for image actions and diagnostics.
- Use `<skill_dir>/scripts/configure_direct.py` for credentials.
- Use `<skill_dir>/scripts/manage.py` for status, updates, and uninstall.

Do not install packages. If Python 3.11+ is unavailable, report that prerequisite instead of changing runtimes or downloading an interpreter.

## Credentials

Read credentials only through the executor. They live at `$CODEX_HOME/podotion-image/provider.toml`, falling back to the current platform's `~/.codex/podotion-image/provider.toml`.

- `PodotionImageSk` is required and handles `auto`, 1K, 2K, and non-4K exact sizes.
- `PodotionImage4kSk` handles explicit 4K-tier requests, canonical 4K-tier dimensions, and exact sizes with at least 8,294,400 pixels.

Legacy files containing only `PodotionImageSk` remain valid for non-4K work. A 4K request without `PodotionImage4kSk` must fail before any output directory, request state, or provider request is created. Never fall back to the default key.

Never print, inspect, return, log, or place either key in an argument. Configure both keys by sending the default key on stdin line 1 and the optional 4K key on line 2 to `configure_direct.py --stdin --force`. To add only the 4K key to a valid legacy config, send one line to `configure_direct.py --set-4k --stdin --force`. Run `podotion_image.py doctor` afterward. Never run `--image-probe` without explicit authorization because it is billable.

Run `doctor` after configuring credentials or when the user explicitly asks for diagnostics. Do not run it before each ordinary image action.

## Resolve size

Translate the user's requested output before calling the executor:

- Use `auto` when no resolution or aspect ratio is requested.
- Use a tier plus ratio for requests expressed as 1K, 2K, or 4K.
- Pass an exact `WIDTHxHEIGHT` when the user gives pixel dimensions; omit `--ratio` in this form.
- With only an aspect ratio, use the 1K tier.

Exact dimensions must have a maximum edge of 3840 pixels, both edges divisible by 16, an aspect ratio no wider than 3:1, and 655,360 through 8,294,400 total pixels. If the request violates a constraint, explain it and ask for another size before making a billable call. Do not silently map valid exact dimensions to a tier.

The executor selects the credential from structured size intent and final dimensions. Do not choose or expose a key in shell code. In particular, exact `2560x1440` uses the default profile, while explicit 4K and `3840x2160` use the 4K profile.

## Request identity

Before the first image action in a task, establish one stable `state_scope`. Prefer the host task or thread ID; otherwise generate one UUID and reuse it for all Podotion actions in that task.

Generate a new UUID `request_key` for each distinct image action. Reuse the same key when checking or recovering that action. Pass `--state-scope` and `--request-key` to every image or recovery command.

Use `--force-new` only when the user explicitly requests an independent variation with otherwise identical inputs. Never rerun a billable command with a new key because a process is quiet, the UI disconnects, or result rendering fails.

## Output location

Resolve the user's save-location intent before making a billable call:

- Use an explicit absolute directory after native-platform normalization.
- Resolve a relative directory from the active project workspace, or the conversation workspace in a projectless task.
- With no requested location, use `<workspace>/PodotionImageOutput`.
- If multiple directories are plausible, ask before calling the script.

Always pass an absolute `--output-dir`. Never resolve output paths from the Skill installation directory.

## Generate and edit

Build one self-contained visual prompt containing only relevant context. Preserve exact visible text. Do not send the whole conversation, secrets, system instructions, or internal reasoning. Send the prompt through stdin.

For generation, run:

```text
podotion_image.py generate --prompt-file - --size <tier-or-WIDTHxHEIGHT> [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> --request-key <key>
```

For editing, use `--last` only when the user unambiguously means the last image in the same state scope and output directory. Otherwise pass one to five absolute `--image` paths. Ask when multiple source images are plausible.

```text
podotion_image.py edit --prompt-file - (--last | --image <path> ...) --size <tier-or-WIDTHxHEIGHT> [--ratio <ratio>] --output-dir <absolute_dir> --state-scope <scope> --request-key <key>
```

Each image action uses one upstream POST with a fixed 600-second timeout and no automatic HTTP retry. Allow the command to remain quiet for several minutes.

## Recovery

After a disconnect or unknown result, run `request-status` with the same `request_key`, `state_scope`, and `output_dir`. Never start another billable request while status is active, unknown, or completed-but-unusable.

Use `request-abandon --acknowledge-possible-charge` only after the user explicitly acknowledges that the uncertain request may already have been billed.

## Deliver results

For every successful item in `images[]`:

1. Embed `images[].markdown_path` as a Markdown image in the final response.
2. Add a separate absolute local file link to `images[].path`.
3. Report structured warnings without turning `ok: true` into a failure.

This standalone Skill does not register MCP `resource_link` entries or publish to the Codex Outputs panel. Never repeat a provider request to improve presentation.

## Manage the Skill

Run lifecycle commands only when the user explicitly requests them:

- Status: `manage.py status`.
- Update: run `manage.py update --dry-run`, then `manage.py update`.
- Legacy Plugin migration: after the standalone Skill passes non-billable `doctor`, run `manage.py uninstall-legacy-plugin --yes`. It removes only the detected `podotion-image@<marketplace>` registration and the exact legacy Marketplace/source entries; it safely skips when the old Plugin is absent and preserves credentials, the new Skill, images, and request state.
- Uninstall: explain that credentials and generated images are preserved, obtain explicit confirmation, then run `manage.py uninstall --yes`.

Do not ask the user to run `codex plugin remove`, edit `marketplace.json`, or delete the legacy source manually. The migration command owns that workflow and refuses same-name entries from another source.

Updates use only the fixed official repository and replace the installed Skill transactionally. After update, migration, or uninstall, tell the user to restart Codex and create a new task. Do not continue using replaced or removed Skill code in the current task.

## Failures

HTTP errors, network disconnects, and provider timeouts are not retried. A failure after submission may already have been billed; preserve its request state and report the sanitized status. Never reveal credentials, authorization headers, full provider configuration, or unsanitized upstream bodies.
