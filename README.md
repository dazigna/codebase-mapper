# codebase-mapper

`codebase-mapper` generates a compact, tree-sitter-based repo map for Python codebases.
The output is designed to be fed to an LLM as a structural index so the model can choose
which files to inspect before you spend context on full source files.

## What it does

- Parses Python files with Tree-sitter.
- Extracts top-level imports, classes, methods, and functions.
- Counts cross-file call references and uses them as ranking signal.
- Resolves internal imports across monorepo-style layouts.
- Builds an internal dependency graph.
- Ranks files with a pure-Python PageRank implementation.
- Emits a token-budgeted repo map for LLM navigation.

## Installation

This project uses `uv` and Python 3.11+.

```bash
uv sync
```

## Usage

Generate a repo map for the current repository:

```bash
uv run python main.py . --tokens 1200 --out repo-map.md
```

Generate a repo map for another repository:

```bash
uv run python main.py /path/to/repo --tokens 1200 --out /path/to/repo-map.md
```

Useful flags:

- `--tokens`: output token budget
- `--out`: write output to a file instead of stdout
- `--exclude`: add extra directories to skip
- `--log`: write parser errors to a log file
- `--focus-file`: boost files matching this path fragment
- `--focus-symbol`: boost files that define or call this symbol

## Testing

```bash
uv run python -m unittest discover -s tests
```

## LLM Workflow

Treat the generated repo map as an index, not as a replacement for source files.

Recommended flow:

1. Generate `repo-map.md`.
2. Give the repo map to the LLM first.
3. Ask the model which files it wants to inspect next.
4. Provide only those files.
5. Repeat with a minimal file set until the model has enough code to act.

## Claude Hook Setup

For a reusable Claude Code hook setup that works with any target repository, see
[docs/claude-hooks.md](docs/claude-hooks.md).
