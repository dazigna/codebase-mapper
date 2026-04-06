# Claude Hook Setup

This guide shows how to wire `codebase-mapper` into Claude Code for any target repository.
The recommended setup is local-only:

- store the hook config in the target repo's `.claude/settings.local.json`
- store the hook scripts in the target repo's `.claude/hooks/`
- keep generated files and local hook state out of git

The examples below assume:

- `codebase-mapper` lives at `/absolute/path/to/codebase-mapper`
- the target repository is whatever Claude opens as `"$CLAUDE_PROJECT_DIR"`

## What the hooks do

1. `SessionStart`
Generates `repo-map.md` for the target repository and injects it into Claude's context.

2. `PostToolUse`
After a Python edit, refreshes `repo-map.md` asynchronously and reminds Claude to consult it.

## 1. Add local Claude settings in the target repo

Create `TARGET_REPO/.claude/settings.local.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/load-repomap.sh",
            "timeout": 90
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/update-repomap.sh",
            "async": true,
            "timeout": 180
          }
        ]
      }
    ]
  }
}
```

## 2. Add the session-start hook

Create `TARGET_REPO/.claude/hooks/load-repomap.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

project_dir="${CLAUDE_PROJECT_DIR:?}"
mapper_dir="/absolute/path/to/codebase-mapper"
map_path="${project_dir}/repo-map.md"
cache_dir="${project_dir}/.claude/.uv-cache"

mkdir -p "${cache_dir}"
export UV_CACHE_DIR="${cache_dir}"

if ! uv run --project "${mapper_dir}" python "${mapper_dir}/main.py" "${project_dir}" --tokens 1200 --out "${map_path}" >/dev/null 2>&1; then
  python3 - <<'PY'
import json
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "Repo map generation failed at session start. Fall back to reading files directly."
    }
}))
PY
  exit 0
fi

python3 - "${map_path}" <<'PY'
import json
import pathlib
import sys

map_path = pathlib.Path(sys.argv[1])
map_text = map_path.read_text(encoding="utf-8")
context = (
    "Read the repo map before navigating this codebase. "
    "Treat it as a structural index, not as the source of truth. "
    f"The current repo map is stored at {map_path}.\n\n"
    f"{map_text}"
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context
    }
}))
PY
```

## 3. Add the post-edit refresh hook

Create `TARGET_REPO/.claude/hooks/update-repomap.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

input_json="$(cat)"
project_dir="${CLAUDE_PROJECT_DIR:?}"
mapper_dir="/absolute/path/to/codebase-mapper"
map_path="${project_dir}/repo-map.md"
cache_dir="${project_dir}/.claude/.uv-cache"

file_path="$(printf '%s' "${input_json}" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
tool_input = data.get("tool_input", {})
tool_response = data.get("tool_response", {})
print(tool_input.get("file_path") or tool_response.get("filePath") or "")
')"

case "${file_path}" in
  *.py) ;;
  *) exit 0 ;;
esac

if [[ "${file_path}" == *"/.claude/"* ]] || [[ "${file_path}" == "${map_path}" ]]; then
  exit 0
fi

mkdir -p "${cache_dir}"
export UV_CACHE_DIR="${cache_dir}"

if ! uv run --project "${mapper_dir}" python "${mapper_dir}/main.py" "${project_dir}" --tokens 1200 --out "${map_path}" >/dev/null 2>&1; then
  exit 0
fi

python3 - "${map_path}" <<'PY'
import json
import pathlib
import sys

map_path = pathlib.Path(sys.argv[1])
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            "Repo map refreshed after a Python edit. "
            f"Updated map: {map_path}. Consult it before further codebase navigation."
        )
    }
}))
PY
```

## 4. Make the hook scripts executable

```bash
chmod +x TARGET_REPO/.claude/hooks/load-repomap.sh
chmod +x TARGET_REPO/.claude/hooks/update-repomap.sh
```

## 5. Ignore local state in the target repo

These files are local and should not be committed.

Either add them to `TARGET_REPO/.git/info/exclude`:

```gitignore
.claude/hooks/
.claude/.uv-cache/
repo-map.md
```

Or manage them however you prefer locally.

## 6. Verify the hooks manually

Verify session-start output:

```bash
env CLAUDE_PROJECT_DIR=/absolute/path/to/target-repo \
bash /absolute/path/to/target-repo/.claude/hooks/load-repomap.sh | python3 -m json.tool
```

Verify refresh output:

```bash
printf '%s' '{"tool_input":{"file_path":"/absolute/path/to/target-repo/app.py"}}' \
| env CLAUDE_PROJECT_DIR=/absolute/path/to/target-repo \
bash /absolute/path/to/target-repo/.claude/hooks/update-repomap.sh | python3 -m json.tool
```

Both commands should emit JSON with `hookSpecificOutput`.

## Notes

- The hook scripts use `uv run --project` so the target repo does not need to vendor `codebase-mapper`.
- `repo-map.md` is generated in the target repo because that is the artifact Claude will keep consulting.
- The repo map is an index, not a substitute for actual source files.

## Claude Code prompt suggestions

Once the hooks are installed, Claude should already receive the repo map at session start.
These prompts help keep the workflow narrow and context-efficient.

### Initial navigation prompt

```text
Use the loaded repo map as a structural index for this repository.

Task:
[describe the task]

Instructions:
- Do not assume implementation details that are not in the source files.
- First identify the 3 to 6 most relevant files to inspect.
- Explain why each file matters.
- Prefer the smallest file set that can confirm the control flow.
- Do not propose code changes yet.
```

### After Claude chooses files

```text
I will provide the requested files next.

When you receive them:
- explain the real control flow
- identify the exact functions, classes, and modules involved
- propose the smallest safe change surface
- request only the next minimal files if something is still missing
```

### Change-planning prompt

```text
Using the loaded repo map and the files already inspected:

- summarize what is known vs inferred
- propose a concrete edit plan
- list risks, edge cases, and tests to update
- keep the change surface as small as possible
```

### Implementation prompt

```text
Implement the change using the smallest safe edit set.

Requirements:
- preserve existing behavior outside the requested change
- avoid unrelated refactors
- update tests only where needed
- if the codebase context is still insufficient, stop and request the next exact files
```

### Debugging prompt

```text
Use the loaded repo map to identify the most likely execution path for this bug:
[describe bug]

Then:
- identify likely entrypoints
- identify likely shared utilities or integrations involved
- request the next minimal files to confirm the hypothesis
- do not guess at runtime behavior without code
```
