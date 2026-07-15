---
name: moneyprinterturbo-video
description: Use this skill when the user wants a finished video from a topic, idea, prompt, or script with MoneyPrinterTurbo. Also use it to install or configure MoneyPrinterTurbo, identify missing API keys, repair failed generation, or locate a generated MP4. The expected outcome is a final video file, not setup instructions.
compatibility: Requires terminal, network, filesystem, and long-running command support. Supports macOS and Windows and uses uv exclusively.
metadata:
  author: "harry0703@hotmail.com"
  version: "1.3.2+custom.5"
  upstream: "https://github.com/harry0703/MoneyPrinterTurbo"
---

# MoneyPrinterTurbo Video Generation

Complete installation, configuration reuse, generation, waiting, and final MP4 delivery automatically. Do not stop after giving commands.

## Required Behavior

1. Ask only for required credentials that are missing or rejected. Combine them into one request.
2. Do not ask for confirmation before installing, generating, waiting, or using defaults.
3. Run the adjacent helper as one foreground command with a timeout of at least 20 minutes.
4. Never poll with repeated process, directory, or log commands. Continue waiting on the same terminal session when supported.
5. Never print API keys, tokens, `config.toml`, or credential-bearing fragments.

## Defaults

Unless requested otherwise, generate one Chinese `9:16` video with Pexels footage, Edge TTS, subtitles, and background music. Install under the user's home directory.

## Execution

Resolve `SKILL_DIR` from this file. Run the adjacent helper by relative name with `workdir=SKILL_DIR`:

```bash
uv run --no-project --python 3.11 python mpt_agent.py --subject "<video topic>"
```

If `uv` is missing, install it and retry once. Do not use Docker, Conda, system pip, or a manually managed virtual environment.

macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Pass additional CLI requirements after `--`. Do not pass `--stop-at`; a Skill request must produce a final video.

## Exit Handling

Exit code 0 prints:

```text
MPT_RESULT
VIDEO_FILE=<absolute path>/final-1.mp4
TASK_DIR=<absolute path>/storage/tasks/<task-id>
LOG_FILE=<absolute path>/run-<task-id>.log
RESULT_FILE=<absolute path>/latest-result.json
```

Return the video path and a concise summary. The helper has already verified that each reported file exists and is non-empty.

Exit code 10 prints `MPT_NEEDS_INPUT` and only the required fields. Ask for those values, set the listed `MPT_*` environment variables, and rerun the same command.

Exit code 1 prints `MPT_ERROR`. Repair a recoverable problem and retry once. If it still fails, report the failed stage, short error, and log path.

## Scope

- Support macOS and Windows only.
- Use uv and the MoneyPrinterTurbo CLI only.
- Do not start Docker, WebUI, or API services.
- Do not run multiple video jobs concurrently.
