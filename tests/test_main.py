import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import RepoMapBuilder


class RepoMapBuilderTests(unittest.TestCase):
    def build_repo(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)

        files = {
            "service/app/__init__.py": "",
            "service/app/core/__init__.py": "",
            "service/app/core/config.py": "SETTINGS = {'env': 'test'}\n",
            "service/app/api/__init__.py": "",
            "service/app/api/routes.py": """
                from app.core.config import SETTINGS
                from shared.telemetry import get_logger

                class Routes:
                    @classmethod
                    def build(cls):
                        return cls()

                def handler():
                    return SETTINGS, get_logger("routes")
            """,
            "libs/shared/shared/__init__.py": "from shared.telemetry import get_logger\n",
            "libs/shared/shared/telemetry/__init__.py": """
                from shared.telemetry.logging import get_logger
            """,
            "libs/shared/shared/telemetry/logging.py": """
                def get_logger(name: str):
                    return name
            """,
        }

        for relative_path, content in files.items():
            file_path = root / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

        return root

    def test_resolves_internal_imports_across_subproject_roots(self) -> None:
        root = self.build_repo()
        builder = RepoMapBuilder(str(root))
        builder.analyze_repo()

        routes_summary = builder.file_data["service/app/api/routes.py"]
        self.assertEqual(routes_summary.module, "app.api.routes")
        self.assertEqual(
            routes_summary.internal_imports,
            [
                "libs/shared/shared/telemetry/__init__.py",
                "service/app/core/config.py",
            ],
        )

    def test_extracts_classes_methods_and_generates_repo_map(self) -> None:
        root = self.build_repo()
        builder = RepoMapBuilder(str(root))
        builder.analyze_repo()

        routes_summary = builder.file_data["service/app/api/routes.py"]
        self.assertEqual([symbol.name for symbol in routes_summary.classes], ["Routes"])
        self.assertEqual(routes_summary.classes[0].methods[0].name, "build")
        self.assertEqual([symbol.name for symbol in routes_summary.functions], ["handler"])

        repo_map = builder.generate_map(token_limit=2000)
        self.assertIn("## Hotspots", repo_map)
        self.assertIn("### service/app/api/routes.py", repo_map)
        self.assertIn("module: app.api.routes", repo_map)
        self.assertIn("methods: L6 build", repo_map)

    def test_reference_counts_increase_graph_edge_weight(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)

        files = {
            "pkg/__init__.py": "",
            "pkg/helpers.py": """
                def helper():
                    return 1
            """,
            "pkg/runner.py": """
                from pkg.helpers import helper

                def run():
                    helper()
                    helper()
                    helper()
            """,
        }

        for relative_path, content in files.items():
            file_path = root / relative_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

        builder = RepoMapBuilder(str(root))
        builder.analyze_repo()

        runner_summary = builder.file_data["pkg/runner.py"]
        self.assertEqual(runner_summary.reference_counts["helper"], 3)
        self.assertEqual(
            builder.graph["pkg/runner.py"]["pkg/helpers.py"]["weight"],
            4.0,
        )

    def test_focus_inputs_boost_ranking(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)

        files = {
            "alpha.py": "def first():\n    return 1\n",
            "beta.py": "def target():\n    return 2\n",
        }

        for relative_path, content in files.items():
            file_path = root / relative_path
            file_path.write_text(content, encoding="utf-8")

        builder = RepoMapBuilder(
            str(root),
            focus_files=["beta.py"],
            focus_symbols=["target"],
        )
        builder.analyze_repo()

        ranked_paths = [path for path, _score in builder._rank_files()]
        self.assertEqual(ranked_paths[0], "beta.py")


if __name__ == "__main__":
    unittest.main()
