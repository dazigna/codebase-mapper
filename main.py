from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx
import tiktoken
import tree_sitter_python
from loguru import logger
from tree_sitter import Language, Node, Parser

PY_LANGUAGE = Language(tree_sitter_python.language())
PYTHON_PARSER = Parser(PY_LANGUAGE)


@dataclass(slots=True)
class ImportSpec:
    module: str
    imported_names: list[str] = field(default_factory=list)
    is_from_import: bool = False


@dataclass(slots=True)
class FunctionSymbol:
    name: str
    line: int
    signature: str
    decorators: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ClassSymbol:
    name: str
    line: int
    signature: str
    decorators: list[str] = field(default_factory=list)
    methods: list[FunctionSymbol] = field(default_factory=list)


@dataclass(slots=True)
class FileSummary:
    path: str
    module: str
    imports: list[ImportSpec] = field(default_factory=list)
    internal_imports: list[str] = field(default_factory=list)
    reference_counts: dict[str, int] = field(default_factory=dict)
    classes: list[ClassSymbol] = field(default_factory=list)
    functions: list[FunctionSymbol] = field(default_factory=list)


class RepoMapBuilder:
    def __init__(
        self,
        root_path: str,
        exclude_dirs: list[str] | None = None,
        focus_files: list[str] | None = None,
        focus_symbols: list[str] | None = None,
    ):
        self.root_path = os.path.abspath(root_path)
        default_exclude = [
            ".git",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "venv",
            "node_modules",
            "client_work",
            "dist",
            "build",
            "htmlcov",
            "coverage",
            "tests",
        ]
        self.exclude_dirs = set(default_exclude + (exclude_dirs or []))
        self.file_data: dict[str, FileSummary] = {}
        self.module_to_paths: dict[str, set[str]] = {}
        self.graph = nx.DiGraph()
        self.tokenizer = tiktoken.get_encoding("cl100k_base")
        self.focus_files = [focus_file.lower() for focus_file in (focus_files or [])]
        self.focus_symbols = [focus_symbol for focus_symbol in (focus_symbols or [])]

    def count_tokens(self, text: str) -> int:
        return len(self.tokenizer.encode(text))

    def analyze_repo(self) -> None:
        for root, dirs, files in os.walk(self.root_path):
            dirs[:] = [
                directory for directory in dirs if directory not in self.exclude_dirs
            ]

            for file_name in files:
                if not file_name.endswith(".py"):
                    continue

                full_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(full_path, self.root_path)
                self._parse_file(full_path, rel_path)

        self.module_to_paths = {}
        for path in self.file_data:
            for alias in self._module_aliases(path):
                self.module_to_paths.setdefault(alias, set()).add(path)
        self._resolve_internal_imports()
        self._build_graph()

    def _parse_file(self, full_path: str, rel_path: str) -> None:
        try:
            code_bytes = Path(full_path).read_bytes()
            code_text = code_bytes.decode("utf-8", errors="ignore")
            lines = code_text.splitlines()
            tree = PYTHON_PARSER.parse(code_bytes)

            summary = FileSummary(
                path=rel_path,
                module=self._display_module_name(rel_path),
            )

            for child in tree.root_node.children:
                if child.type == "import_statement":
                    summary.imports.extend(self._parse_import_statement(child))
                elif child.type == "import_from_statement":
                    summary.imports.append(self._parse_import_from_statement(child))
                elif child.type == "class_definition":
                    summary.classes.append(self._parse_class_symbol(child, lines))
                elif child.type == "function_definition":
                    summary.functions.append(self._parse_function_symbol(child, lines))
                elif child.type == "decorated_definition":
                    self._parse_decorated_definition(child, lines, summary)

            summary.reference_counts = self._collect_reference_counts(tree.root_node)
            self.file_data[rel_path] = summary
        except Exception as exc:
            logger.error(f"Failed to parse {rel_path}: {exc}")

    def _parse_decorated_definition(
        self, node: Node, lines: list[str], summary: FileSummary
    ) -> None:
        decorators = [
            child.text.decode("utf-8", errors="ignore").strip()
            for child in node.children
            if child.type == "decorator"
        ]
        definition = next(
            (
                child
                for child in node.children
                if child.type in {"class_definition", "function_definition"}
            ),
            None,
        )
        if definition is None:
            return

        if definition.type == "class_definition":
            summary.classes.append(
                self._parse_class_symbol(definition, lines, decorators)
            )
        elif definition.type == "function_definition":
            summary.functions.append(
                self._parse_function_symbol(definition, lines, decorators)
            )

    def _parse_import_statement(self, node: Node) -> list[ImportSpec]:
        imports: list[ImportSpec] = []

        for child in node.children:
            if child.type == "dotted_name":
                imports.append(ImportSpec(module=child.text.decode("utf-8")))
            elif child.type == "aliased_import":
                module_node = next(
                    (
                        grandchild
                        for grandchild in child.children
                        if grandchild.type == "dotted_name"
                    ),
                    None,
                )
                if module_node is not None:
                    imports.append(ImportSpec(module=module_node.text.decode("utf-8")))

        return imports

    def _parse_import_from_statement(self, node: Node) -> ImportSpec:
        module_node = next(
            (
                child
                for child in node.children
                if child.type in {"dotted_name", "relative_import"}
            ),
            None,
        )
        imported_names: list[str] = []
        before_import_keyword = True

        for child in node.children:
            if child.type == "import":
                before_import_keyword = False
                continue

            if before_import_keyword:
                continue

            if child.type == "dotted_name":
                imported_names.append(child.text.decode("utf-8"))
            elif child.type == "aliased_import":
                name_node = next(
                    (
                        grandchild
                        for grandchild in child.children
                        if grandchild.type == "dotted_name"
                    ),
                    None,
                )
                if name_node is not None:
                    imported_names.append(name_node.text.decode("utf-8"))
            elif child.type == "wildcard_import":
                imported_names.append("*")

        return ImportSpec(
            module=module_node.text.decode("utf-8") if module_node is not None else "",
            imported_names=imported_names,
            is_from_import=True,
        )

    def _parse_class_symbol(
        self, node: Node, lines: list[str], decorators: list[str] | None = None
    ) -> ClassSymbol:
        name_node = node.child_by_field_name("name")
        methods: list[FunctionSymbol] = []
        block = next((child for child in node.children if child.type == "block"), None)

        if block is not None:
            for child in block.children:
                method = self._parse_method_symbol(child, lines)
                if method is not None:
                    methods.append(method)

        return ClassSymbol(
            name=name_node.text.decode("utf-8")
            if name_node is not None
            else "<anonymous>",
            line=node.start_point[0] + 1,
            signature=self._signature_for_node(node, lines),
            decorators=list(decorators or []),
            methods=methods,
        )

    def _parse_method_symbol(
        self, node: Node, lines: list[str]
    ) -> FunctionSymbol | None:
        if node.type == "function_definition":
            return self._parse_function_symbol(node, lines)

        if node.type != "decorated_definition":
            return None

        decorators = [
            child.text.decode("utf-8", errors="ignore").strip()
            for child in node.children
            if child.type == "decorator"
        ]
        definition = next(
            (child for child in node.children if child.type == "function_definition"),
            None,
        )
        if definition is None:
            return None

        return self._parse_function_symbol(definition, lines, decorators)

    def _parse_function_symbol(
        self, node: Node, lines: list[str], decorators: list[str] | None = None
    ) -> FunctionSymbol:
        name_node = node.child_by_field_name("name")
        return FunctionSymbol(
            name=name_node.text.decode("utf-8")
            if name_node is not None
            else "<anonymous>",
            line=node.start_point[0] + 1,
            signature=self._signature_for_node(node, lines),
            decorators=list(decorators or []),
        )

    def _signature_for_node(self, node: Node, lines: list[str]) -> str:
        line_index = node.start_point[0]
        if 0 <= line_index < len(lines):
            return lines[line_index].strip()

        return node.text.decode("utf-8", errors="ignore").splitlines()[0].strip()

    def _collect_reference_counts(self, node: Node) -> dict[str, int]:
        reference_counts: defaultdict[str, int] = defaultdict(int)

        def walk(current: Node) -> None:
            if current.type == "call":
                function_node = current.child_by_field_name("function")
                for name in self._extract_reference_names(function_node):
                    reference_counts[name] += 1

            for child in current.children:
                walk(child)

        walk(node)
        return dict(sorted(reference_counts.items()))

    def _extract_reference_names(self, node: Node | None) -> list[str]:
        if node is None:
            return []

        if node.type == "identifier":
            return [node.text.decode("utf-8")]

        if node.type == "attribute":
            attribute_node = node.child_by_field_name("attribute")
            if attribute_node is not None:
                return [attribute_node.text.decode("utf-8")]

            identifier_children = [child for child in node.children if child.type == "identifier"]
            if identifier_children:
                return [identifier_children[-1].text.decode("utf-8")]

        if node.type == "call":
            return self._extract_reference_names(node.child_by_field_name("function"))

        return []

    def _package_parts(self, rel_path: str, module_name: str) -> list[str]:
        if not module_name:
            return []

        module_parts = module_name.split(".")
        if Path(rel_path).name == "__init__.py":
            return module_parts
        return module_parts[:-1]

    def _resolve_relative_module(
        self, module_name: str, rel_path: str, current_module: str
    ) -> str:
        if not module_name.startswith("."):
            return module_name

        leading_dots = len(module_name) - len(module_name.lstrip("."))
        suffix = module_name[leading_dots:]
        package_parts = self._package_parts(rel_path, current_module)
        levels_up = max(leading_dots - 1, 0)

        if levels_up > len(package_parts):
            return suffix

        base_parts = package_parts[: len(package_parts) - levels_up]
        if suffix:
            base_parts.extend(part for part in suffix.split(".") if part)

        return ".".join(base_parts)

    def _module_alias_candidates(self, rel_path: str) -> list[tuple[int, str]]:
        module_path = Path(rel_path).with_suffix("")
        parts = list(module_path.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]

        aliases: list[tuple[int, str]] = []
        for start in range(len(parts)):
            candidate_parts = parts[start:]
            if candidate_parts and all(part.isidentifier() for part in candidate_parts):
                aliases.append((start, ".".join(candidate_parts)))
        return aliases

    def _module_aliases(self, rel_path: str) -> list[str]:
        return [alias for _start, alias in self._module_alias_candidates(rel_path)]

    def _display_module_name(self, rel_path: str) -> str:
        alias_candidates = self._module_alias_candidates(rel_path)
        aliases = [alias for _start, alias in alias_candidates]
        if not aliases:
            return ""

        module_path = Path(rel_path).with_suffix("")
        parts = list(module_path.parts)
        is_package_init = bool(parts and parts[-1] == "__init__")
        if is_package_init:
            parts = parts[:-1]

        valid_package_aliases: list[tuple[int, str]] = []
        final_package_index = len(parts) if is_package_init else len(parts) - 1

        for start, alias in alias_candidates:
            package_indexes = range(start, final_package_index)
            if all(
                Path(self.root_path, *parts[: index + 1], "__init__.py").exists()
                for index in package_indexes
            ):
                valid_package_aliases.append((start, alias))

        for start, alias in valid_package_aliases:
            previous_is_package = (
                start > 0
                and Path(self.root_path, *parts[:start], "__init__.py").exists()
            )
            if not previous_is_package:
                return alias

        if valid_package_aliases:
            return valid_package_aliases[0][1]

        return aliases[0]

    def _module_candidates(self, module_name: str) -> list[str]:
        if not module_name:
            return []

        parts = [part for part in module_name.split(".") if part]
        return [".".join(parts[:index]) for index in range(len(parts), 0, -1)]

    def _select_target_path(
        self, candidates: set[str], current_path: str
    ) -> str | None:
        if not candidates:
            return None

        if len(candidates) == 1:
            return next(iter(candidates))

        current_root = Path(current_path).parts[0]
        same_root = sorted(
            path
            for path in candidates
            if Path(path).parts and Path(path).parts[0] == current_root
        )
        if len(same_root) == 1:
            return same_root[0]

        return sorted(candidates)[0]

    def _resolve_import_targets(
        self, summary: FileSummary, import_spec: ImportSpec
    ) -> list[str]:
        resolved_paths: list[str] = []
        seen_paths: set[str] = set()

        def add_module_candidates(module_name: str) -> None:
            for candidate in self._module_candidates(module_name):
                matched_paths = self.module_to_paths.get(candidate, set())
                target_path = self._select_target_path(matched_paths, summary.path)
                if target_path:
                    if target_path != summary.path and target_path not in seen_paths:
                        seen_paths.add(target_path)
                        resolved_paths.append(target_path)
                    break

        if import_spec.is_from_import:
            base_module = self._resolve_relative_module(
                import_spec.module, summary.path, summary.module
            )
            if not import_spec.imported_names:
                add_module_candidates(base_module)
                return resolved_paths

            for imported_name in import_spec.imported_names:
                if imported_name == "*":
                    add_module_candidates(base_module)
                    continue

                if base_module:
                    add_module_candidates(f"{base_module}.{imported_name}")
                add_module_candidates(base_module)
        else:
            add_module_candidates(import_spec.module)

        return resolved_paths

    def _resolve_internal_imports(self) -> None:
        for summary in self.file_data.values():
            internal_imports: list[str] = []
            seen: set[str] = set()

            for import_spec in summary.imports:
                for target_path in self._resolve_import_targets(summary, import_spec):
                    if target_path not in seen:
                        seen.add(target_path)
                        internal_imports.append(target_path)

            summary.internal_imports = sorted(internal_imports)

    def _defined_symbols(self, summary: FileSummary) -> set[str]:
        defined_symbols = {function_symbol.name for function_symbol in summary.functions}

        for class_symbol in summary.classes:
            defined_symbols.add(class_symbol.name)
            for method_symbol in class_symbol.methods:
                defined_symbols.add(method_symbol.name)
                defined_symbols.add(f"{class_symbol.name}.{method_symbol.name}")

        return defined_symbols

    def _build_graph(self) -> None:
        self.graph = nx.DiGraph()
        edge_weights: defaultdict[tuple[str, str], float] = defaultdict(float)
        symbol_to_paths: defaultdict[str, set[str]] = defaultdict(set)

        for path in self.file_data:
            self.graph.add_node(path)

        for path, summary in self.file_data.items():
            for target_path in summary.internal_imports:
                edge_weights[(path, target_path)] += 1.0

            for symbol_name in self._defined_symbols(summary):
                symbol_to_paths[symbol_name].add(path)

        for path, summary in self.file_data.items():
            for symbol_name, reference_count in summary.reference_counts.items():
                target_paths = sorted(
                    target_path
                    for target_path in symbol_to_paths.get(symbol_name, set())
                    if target_path != path
                )
                if not target_paths:
                    continue

                shared_weight = reference_count / len(target_paths)
                for target_path in target_paths:
                    edge_weights[(path, target_path)] += shared_weight

        for (source_path, target_path), weight in edge_weights.items():
            self.graph.add_edge(source_path, target_path, weight=weight)

    def _pagerank(
        self, alpha: float = 0.85, max_iter: int = 100, tol: float = 1.0e-6
    ) -> dict[str, float]:
        nodes = list(self.graph.nodes())
        if not nodes:
            return {}

        node_count = len(nodes)
        scores = {node: 1.0 / node_count for node in nodes}
        outgoing_weight = {
            node: sum(
                self.graph[node][target]["weight"] for target in self.graph.successors(node)
            )
            for node in nodes
        }
        dangling_nodes = [
            node for node, total_weight in outgoing_weight.items() if total_weight == 0
        ]

        for _ in range(max_iter):
            previous_scores = scores
            dangling_share = (
                alpha
                * sum(previous_scores[node] for node in dangling_nodes)
                / node_count
            )
            scores = {
                node: ((1.0 - alpha) / node_count) + dangling_share for node in nodes
            }

            for source in nodes:
                total_weight = outgoing_weight[source]
                if total_weight == 0:
                    continue

                for target in self.graph.successors(source):
                    edge_weight = self.graph[source][target]["weight"]
                    scores[target] += (
                        alpha * previous_scores[source] * edge_weight / total_weight
                    )

            error = sum(abs(scores[node] - previous_scores[node]) for node in nodes)
            if error < node_count * tol:
                break

        return scores

    def _focus_score(self, path: str) -> float:
        summary = self.file_data[path]
        defined_symbols = self._defined_symbols(summary)
        referenced_symbols = set(summary.reference_counts)
        normalized_path = path.lower()
        base_name = Path(path).name.lower()
        score = 0.0

        for focus_file in self.focus_files:
            if (
                normalized_path == focus_file
                or normalized_path.endswith(focus_file)
                or base_name == focus_file
            ):
                score += 8.0
            elif focus_file in normalized_path or focus_file in base_name:
                score += 4.0

        for focus_symbol in self.focus_symbols:
            if focus_symbol in defined_symbols:
                score += 6.0
            elif focus_symbol in referenced_symbols:
                score += 2.0

        return score

    def _rank_files(self) -> list[tuple[str, float]]:
        if not self.graph.nodes:
            return []

        rankings = self._pagerank()
        boosted_rankings = {
            path: score + self._focus_score(path) for path, score in rankings.items()
        }
        return sorted(boosted_rankings.items(), key=lambda item: (-item[1], item[0]))

    def _hotspot_line(self, path: str, score: float) -> str:
        summary = self.file_data[path]
        return (
            f"- {path} | score={score:.4f} | imported_by={self.graph.in_degree(path)} "
            f"| imports={self.graph.out_degree(path)} | calls={sum(summary.reference_counts.values())} "
            f"| classes={len(summary.classes)} | functions={len(summary.functions)}"
        )

    def _format_class(self, symbol: ClassSymbol) -> list[str]:
        lines = [f"  - L{symbol.line}: {symbol.signature}"]
        if symbol.decorators:
            lines.append(f"    decorators: {', '.join(symbol.decorators)}")
        if symbol.methods:
            method_parts = [
                f"L{method.line} {method.name}" for method in symbol.methods
            ]
            lines.append(f"    methods: {', '.join(method_parts)}")
        return lines

    def _format_function(self, symbol: FunctionSymbol) -> list[str]:
        lines = [f"  - L{symbol.line}: {symbol.signature}"]
        if symbol.decorators:
            lines.append(f"    decorators: {', '.join(symbol.decorators)}")
        return lines

    def _format_file_section(self, path: str) -> str:
        summary = self.file_data[path]
        dependents = sorted(self.graph.predecessors(path))

        lines = [f"### {path}", f"module: {summary.module or '<root>'}"]
        lines.append(
            "depends_on: "
            + (
                ", ".join(summary.internal_imports)
                if summary.internal_imports
                else "<none>"
            )
        )
        lines.append(
            "depended_on_by: " + (", ".join(dependents[:8]) if dependents else "<none>")
        )

        if summary.classes:
            lines.append("classes:")
            for class_symbol in summary.classes:
                lines.extend(self._format_class(class_symbol))

        if summary.functions:
            lines.append("functions:")
            for function_symbol in summary.functions:
                lines.extend(self._format_function(function_symbol))

        if not summary.classes and not summary.functions:
            lines.append("symbols: <none>")

        return "\n".join(lines) + "\n\n"

    def generate_map(self, token_limit: int = 4096) -> str:
        ranked_files = self._rank_files()

        header_lines = [
            "# Repo Map",
            f"root: {self.root_path}",
            f"python_files: {len(self.file_data)}",
            f"internal_import_edges: {self.graph.number_of_edges()}",
            "",
            "## Hotspots",
        ]
        output_sections = ["\n".join(header_lines) + "\n"]
        current_tokens = self.count_tokens(output_sections[0])

        for path, score in ranked_files[:20]:
            line = self._hotspot_line(path, score) + "\n"
            line_tokens = self.count_tokens(line)
            if current_tokens + line_tokens > token_limit:
                return "".join(output_sections)
            output_sections.append(line)
            current_tokens += line_tokens

        separator = "\n## Files\n\n"
        separator_tokens = self.count_tokens(separator)
        if current_tokens + separator_tokens > token_limit:
            return "".join(output_sections)

        output_sections.append(separator)
        current_tokens += separator_tokens

        for path, _score in ranked_files:
            section = self._format_file_section(path)
            section_tokens = self.count_tokens(section)
            if current_tokens + section_tokens > token_limit:
                logger.info(f"Token limit reached at {path}")
                break
            output_sections.append(section)
            current_tokens += section_tokens

        return "".join(output_sections)


def main() -> None:
    cli = argparse.ArgumentParser(
        description="Generate a Python Tree-sitter repo map for LLM navigation."
    )
    cli.add_argument(
        "root", nargs="?", default=".", help="Root directory of the project"
    )
    cli.add_argument(
        "--tokens", type=int, default=4096, help="Token budget for the output"
    )
    cli.add_argument("--out", type=str, help="Output file (default: stdout)")
    cli.add_argument(
        "--log", type=str, default="repomap.log", help="File to store error logs"
    )
    cli.add_argument("--exclude", nargs="+", help="Additional directories to exclude")
    cli.add_argument(
        "--focus-file",
        action="append",
        default=[],
        help="Boost ranking for files matching this path fragment. Can be passed multiple times.",
    )
    cli.add_argument(
        "--focus-symbol",
        action="append",
        default=[],
        help="Boost ranking for files that define or call this symbol. Can be passed multiple times.",
    )

    args = cli.parse_args()

    logger.remove()
    logger.add(args.log, level="ERROR", rotation="5 MB")

    builder = RepoMapBuilder(
        args.root,
        exclude_dirs=args.exclude,
        focus_files=args.focus_file,
        focus_symbols=args.focus_symbol,
    )

    print(f"--- Scanning {os.path.abspath(args.root)} ---", file=sys.stderr)
    builder.analyze_repo()

    print(f"--- Generating Map (Budget: {args.tokens} tokens) ---", file=sys.stderr)
    repo_map = builder.generate_map(token_limit=args.tokens)

    if args.out:
        Path(args.out).write_text(repo_map, encoding="utf-8")
        print(
            f"Success! Map saved to {args.out}. Errors logged to {args.log}",
            file=sys.stderr,
        )
        return

    print(repo_map)


if __name__ == "__main__":
    main()
