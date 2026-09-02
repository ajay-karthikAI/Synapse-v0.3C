"""
Static guarantees about the codebase itself.

Three properties are asserted here by parsing source rather than by running it,
so they hold even for code paths no test exercises:

1. **No runtime path deserialises pickle.** Only the quarantined migration
   reader may import ``pickle``, and only the migration CLI may import it.
2. **No dynamic execution.** No ``eval``, ``exec``, ``compile``,
   ``__import__``, ``importlib.import_module``, ``os.system``, ``marshal`` or
   unsafe YAML loading anywhere in the package.
3. **Imports work on a case-sensitive filesystem.** Every ``synapse`` module
   imports by its canonical dotted path, and no two files differ only in case.

The final test is a *characterisation* test: it records the case-sensitivity
defects that already exist in the legacy application modules, which are out of
scope for this change. It is written to fail if those are fixed, so whoever
fixes them updates this list deliberately.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pkgutil
import warnings  # Needed to parse legacy modules that carry an invalid escape sequence
from pathlib import Path
from typing import ClassVar  # Marks test-class constants as class-level, not instance defaults

import pytest

import synapse
from tests.conftest import REPO_ROOT

PACKAGE_ROOT = Path(synapse.__file__).resolve().parent

QUARANTINED_MODULE = "synapse/_legacy/pickle_reader.py"  # The ONE file permitted to import pickle
PERMITTED_PICKLE_IMPORTERS = {QUARANTINED_MODULE}

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}  # Dynamic execution primitives
FORBIDDEN_ATTRIBUTE_CALLS = {  # (module, attribute) pairs that execute or deserialise unsafely
    ("importlib", "import_module"),
    ("os", "system"),
    ("os", "popen"),
    ("pickle", "load"),
    ("pickle", "loads"),
    ("marshal", "load"),
    ("marshal", "loads"),
    ("yaml", "load"),  # Unsafe unless yaml.safe_load; banned outright since no YAML is used
}


def _package_source_files() -> list[Path]:
    """Every .py file in the synapse package."""
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _relative(path: Path) -> str:
    """Repository-relative POSIX path, for readable assertion messages."""
    return path.relative_to(REPO_ROOT).as_posix()


class TestNoPickleInRuntimePaths:
    """Requirement: new runtime application paths must never deserialize pickle."""

    def test_only_the_quarantined_module_imports_pickle(self) -> None:
        offenders = []
        for source_file in _package_source_files():
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imports_pickle = (
                    isinstance(node, ast.Import)
                    and any(alias.name.split(".")[0] == "pickle" for alias in node.names)
                ) or (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").split(".")[0] == "pickle"
                )
                if imports_pickle and _relative(source_file) not in PERMITTED_PICKLE_IMPORTERS:
                    offenders.append(_relative(source_file))
        assert offenders == [], f"pickle imported outside the quarantine: {offenders}"

    def test_only_the_migration_cli_imports_the_quarantined_reader(self) -> None:
        # Enforces the import-graph contract: the pickle reader is reachable
        # from exactly one command, not from anything the app serves from.
        allowed = {"synapse/cli/migrate_artifacts.py", QUARANTINED_MODULE}
        offenders = []
        for source_file in _package_source_files():
            if _relative(source_file) in allowed:
                continue
            if "_legacy.pickle_reader" in source_file.read_text(encoding="utf-8"):
                offenders.append(_relative(source_file))
        assert offenders == [], (
            f"quarantined reader referenced outside the migration CLI: {offenders}"
        )

    def test_corpus_and_index_layers_are_pickle_free(self) -> None:
        # The layers the application actually loads from at runtime.
        #
        # Checked over the parsed AST rather than the raw text: these modules
        # discuss pickle at length in their docstrings (explaining why the
        # format was abandoned), so a substring search would match prose. What
        # matters is whether any *code* references it.
        for subpackage in ("corpus", "index", "schemas"):
            for source_file in (PACKAGE_ROOT / subpackage).rglob("*.py"):
                tree = ast.parse(source_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        assert all(a.name.split(".")[0] != "pickle" for a in node.names), _relative(
                            source_file
                        )
                    elif isinstance(node, ast.ImportFrom):
                        assert (node.module or "").split(".")[0] != "pickle", _relative(source_file)
                    elif isinstance(node, ast.Name):
                        assert node.id != "pickle", f"{_relative(source_file)}:{node.lineno}"


class TestNoDynamicExecution:
    """Requirement: no eval, exec, dynamic imports, or unsafe YAML loading."""

    def test_no_forbidden_builtin_calls(self) -> None:
        offenders = []
        for source_file in _package_source_files():
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in FORBIDDEN_CALLS
                ):
                    offenders.append(f"{_relative(source_file)}:{node.lineno} {node.func.id}")
        assert offenders == [], f"dynamic execution primitives found: {offenders}"

    def test_no_forbidden_attribute_calls(self) -> None:
        offenders = []
        for source_file in _package_source_files():
            if _relative(source_file) == QUARANTINED_MODULE:
                continue  # The quarantined reader constructs a restricted Unpickler; it never calls pickle.load/loads
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    value = node.func.value
                    if (
                        isinstance(value, ast.Name)
                        and (value.id, node.func.attr) in FORBIDDEN_ATTRIBUTE_CALLS
                    ):
                        offenders.append(
                            f"{_relative(source_file)}:{node.lineno} {value.id}.{node.func.attr}"
                        )
        assert offenders == [], f"unsafe calls found: {offenders}"

    def test_quarantined_reader_never_calls_pickle_load_directly(self) -> None:
        # It must go through _RestrictedUnpickler, whose find_class enforces the
        # allow-list. A bare pickle.load would bypass that entirely.
        source = (PACKAGE_ROOT / "_legacy" / "pickle_reader.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                value = node.func.value
                if (
                    isinstance(value, ast.Name)
                    and value.id == "pickle"
                    and node.func.attr in {"load", "loads"}
                ):
                    pytest.fail(
                        f"pickle.{node.func.attr} called directly at line {node.lineno}; must use _RestrictedUnpickler"
                    )

    def test_no_yaml_dependency_at_all(self) -> None:
        for source_file in _package_source_files():
            assert "import yaml" not in source_file.read_text(encoding="utf-8"), _relative(
                source_file
            )


class TestCaseSensitiveImports:
    """Requirement: imports must work on a case-sensitive Linux filesystem."""

    def test_every_synapse_module_imports_by_canonical_path(self) -> None:
        # The direct regression test for the defect class in the legacy code:
        # on Linux an incorrectly-cased import raises ModuleNotFoundError.
        imported = []
        for module in pkgutil.walk_packages([str(PACKAGE_ROOT)], prefix="synapse."):
            importlib.import_module(module.name)
            imported.append(module.name)
        assert len(imported) >= 12, f"expected the full package to import, got {imported}"

    def test_all_synapse_module_paths_are_lowercase(self) -> None:
        # A lowercase-only tree cannot develop a case-mismatch bug.
        offenders = [
            _relative(p)
            for p in _package_source_files()
            if any(part != part.lower() for part in p.relative_to(PACKAGE_ROOT).parts)
        ]
        assert offenders == [], f"non-lowercase module paths: {offenders}"

    def test_no_two_repository_paths_differ_only_by_case(self) -> None:
        # Such a pair works on macOS and breaks on Linux, or silently resolves
        # to the wrong file.
        seen: dict[str, str] = {}
        collisions = []
        for path in REPO_ROOT.rglob("*.py"):
            if any(part in {".venv", "__pycache__", "build", "dist"} for part in path.parts):
                continue
            key = str(path.relative_to(REPO_ROOT)).lower()
            if key in seen and seen[key] != str(path.relative_to(REPO_ROOT)):
                collisions.append((seen[key], str(path.relative_to(REPO_ROOT))))
            seen[key] = str(path.relative_to(REPO_ROOT))
        assert collisions == [], f"paths differing only by case: {collisions}"

    def test_synapse_package_is_importable_without_sys_path_mutation(self) -> None:
        # The legacy modules each prepend the repo root to sys.path at import
        # time, which is what let the case bugs hide. An installed package needs
        # no such trick.
        source = (PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8")
        assert "sys.path" not in source
        assert importlib.util.find_spec("synapse.corpus.jsonl") is not None


class TestKnownLegacyCaseDefects:
    """Characterisation test for defects this change deliberately does not fix.

    ``app.py`` and ``Generation/answer_generator.py`` import lowercase module
    paths that do not exist on disk. Both sit inside broad ``except`` handlers
    or in an unused function, so they fail silently today. They are NOT fixed
    here: repairing the ``app.py`` evaluator import would reconnect a logger
    that writes raw patient query text to disk, which must not happen before a
    privacy-safe telemetry layer exists (docs/quality-architecture.md §1.6, §5).

    This test pins the exact known set so a new occurrence fails, and so fixing
    one is a deliberate act that updates this list.
    """

    # All four previously-known defects are FIXED as of the evaluation-harness
    # change, so this set is now empty and the test asserts that no NEW
    # case-mismatched import appears. It was written to fail when the set
    # changed in either direction, and that is what forced this update.
    #
    # What was fixed:
    #   app.py                          "evaluation.evaluator"   -> removed; the
    #       sidebar now reads the harness's summary.json, and the live-query
    #       scoring call (empty ground truth) was deleted outright.
    #   Generation/answer_generator.py  "retrieval.*"            -> corrected to
    #       the real "Retrieval" casing; two of the three imports were unused.
    KNOWN_BAD: ClassVar[set[tuple[str, str]]] = set()

    LEGACY_PACKAGE_DIRS: ClassVar[set[str]] = {"Data", "Retrieval", "Generation", "Evaluation"}

    def _unresolvable_local_imports(self) -> set[tuple[str, str]]:
        """Find imports naming a local package whose case does not match disk."""
        on_disk = {d.lower(): d for d in self.LEGACY_PACKAGE_DIRS}
        found: set[tuple[str, str]] = set()
        targets = [REPO_ROOT / "app.py", REPO_ROOT / "build_corpus.py"]
        targets += [p for d in self.LEGACY_PACKAGE_DIRS for p in (REPO_ROOT / d).rglob("*.py")]
        for source_file in targets:
            if not source_file.is_file():
                continue
            # Warnings are suppressed around the parse because Retrieval/reranker.py
            # contains an invalid escape sequence ('\' followed by a space at
            # line 44), which raises a DeprecationWarning on 3.11 — escalated to
            # an error by this project's filterwarnings setting. That is a real
            # latent defect in the legacy module, tracked separately and NOT
            # fixed here; suppressing it keeps this test measuring what it is
            # about (import casing) rather than failing for an unrelated reason.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if not module:
                    continue
                head = module.split(".")[0]
                if head.lower() in on_disk and head != on_disk[head.lower()]:
                    found.add((source_file.relative_to(REPO_ROOT).as_posix(), module))
        return found

    def test_known_case_mismatched_imports_are_exactly_as_documented(self) -> None:
        # Skip rather than fail when the legacy tree is absent. The synapse
        # package is independently installable, so it is legitimate to run this
        # suite against a checkout containing only synapse/ and tests/ — in
        # which case there is no legacy code to characterise.
        if not (REPO_ROOT / "app.py").is_file():
            pytest.skip("legacy application tree not present in this checkout")
        found = self._unresolvable_local_imports()
        assert found == self.KNOWN_BAD, (
            "The set of case-mismatched legacy imports changed.\n"
            f"  newly broken: {sorted(found - self.KNOWN_BAD)}\n"
            f"  now fixed:    {sorted(self.KNOWN_BAD - found)}\n"
            "Update KNOWN_BAD deliberately; see docs/quality-architecture.md §5 for sequencing."
        )

    def test_the_new_package_contains_no_such_defects(self) -> None:
        # Whatever the legacy tree does, synapse itself must be clean.
        for source_file in _package_source_files():
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module and module.split(".")[0] in self.LEGACY_PACKAGE_DIRS:
                    pytest.fail(f"{_relative(source_file)} imports legacy package {module}")


class TestLoggingCallsAreValid:
    """Structured logging must not collide with reserved LogRecord attributes.

    Regression guard. ``extra={"created": ...}`` raises
    ``KeyError: Attempt to overwrite 'created' in LogRecord`` at call time, not
    import time, so it hides in any branch a test does not exercise. This check
    is static and therefore covers every branch.
    """

    RESERVED_LOG_RECORD_KEYS: ClassVar[set[str]] = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }

    def test_no_extra_dict_uses_a_reserved_key(self) -> None:
        offenders = []
        for source_file in _package_source_files():
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                        continue
                    for key in keyword.value.keys:
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in self.RESERVED_LOG_RECORD_KEYS
                        ):
                            offenders.append(
                                f"{_relative(source_file)}:{node.lineno} extra['{key.value}']"
                            )
        assert offenders == [], (
            f"logging extra= collides with reserved LogRecord attributes: {offenders}"
        )


class TestPatientTextIsNotLogged:
    """Patient query text must never reach a log record.

    Added during the 2026-08-14 audit. Both SECURITY.md and docs/VALIDATION.md
    asserted this property, and docs/PRIVACY_DATA_FLOW.md depends on it, but
    nothing enforced it — synapse/logging.py documented the intent in a
    docstring and that was all. A documented invariant with no test is a
    convention, and conventions do not survive contact with a deadline.

    Static rather than runtime, for the same reason as the check above: a log
    call on an un-exercised error branch is exactly where patient text leaks,
    and a runtime test only covers the branches it happens to run.

    The rule is on the KEY, not the value: a structured field named `query`,
    `question`, `text`, `answer` or similar carries free text a patient typed.
    Identifiers, counts and hashes are fine — those are what structured logging
    is for.
    """

    # Field names that carry (or may carry) free text originating from a user.
    FORBIDDEN_LOG_FIELDS: ClassVar[set[str]] = {
        "query",
        "queries",
        "query_text",
        "question",
        "user_input",
        "patient_query",
        "text",
        "body",
        "content",
        "answer",
        "answer_text",
        "excerpt",
        "claim",
        "prompt",
        "completion",
        "normalized_query",
    }

    # Suffixes that make an otherwise-forbidden name safe: these are derived
    # values, not the text itself. `query_sha256` and `query_length` disclose
    # nothing a log reader could reconstruct the question from.
    SAFE_SUFFIXES: ClassVar[tuple[str, ...]] = (
        "_sha256",
        "_hash",
        "_digest",
        "_length",
        "_len",
        "_count",
        "_id",
    )

    def _is_forbidden(self, field: str) -> bool:
        """True if a structured-logging field name may carry patient text."""
        if field.endswith(self.SAFE_SUFFIXES):  # a hash or a count, never the text
            return False
        return field in self.FORBIDDEN_LOG_FIELDS

    def test_no_log_call_passes_patient_text_as_a_structured_field(self) -> None:
        offenders = []
        for source_file in _package_source_files():
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                        continue
                    for key in keyword.value.keys:
                        if isinstance(key, ast.Constant) and self._is_forbidden(str(key.value)):
                            offenders.append(
                                f"{_relative(source_file)}:{node.lineno} extra['{key.value}']"
                            )
        assert offenders == [], (
            "patient text must not be logged; hash or measure it instead "
            f"(e.g. query_sha256, query_length): {offenders}"
        )

    def test_the_guard_itself_rejects_a_known_bad_field(self) -> None:
        """Guard the guard.

        A static check that silently stops matching is worse than no check, so
        assert the predicate still classifies both directions correctly.
        """
        assert self._is_forbidden("query") is True
        assert self._is_forbidden("normalized_query") is True
        assert self._is_forbidden("query_sha256") is False  # a digest, not the text
        assert self._is_forbidden("query_length") is False  # a measurement
        assert self._is_forbidden("case_id") is False  # an identifier
