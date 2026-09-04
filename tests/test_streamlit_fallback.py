"""
Streamlit is a fallback over the same service, not a second implementation.

Next.js over the FastAPI contract is the primary interface. Streamlit remains
supported for local use, and the whole basis of that support is that it renders
decisions it did not make: the emergency check, retrieval, verification and the
failure taxonomy all live in :mod:`synapse.service` and its dependencies, and
``app.py`` only draws the result.

That is a claim which rots silently. A future change that reached for
``rank_bm25`` or wrote a quick keyword check directly in ``app.py`` would work,
would look reasonable in review, and would mean the two interfaces could answer
the same question differently -- with the safety-critical one being whichever
the reader happened not to be looking at. So it is asserted here.

Imports are read with :mod:`ast` rather than matched as text. A file that
*documents* the thing it forbids must not read as a file that breaks the rule
(the same trap ``tests/test_no_pickle_and_imports.py`` is careful about), and
parsing the syntax tree removes the question entirely.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The prototype trees. `synapse` must never import them; `app.py` may reach them
# only through the quarantined `legacy_index` seam at the repository root.
LEGACY_TREES = {"Data", "Retrieval", "Generation", "Evaluation"}

# Retrieval and provider libraries. `app.py` importing any of these directly
# would mean it had begun doing retrieval rather than displaying it.
RETRIEVAL_STACK = {"faiss", "rank_bm25", "numpy", "openai", "langchain_text_splitters"}


def imported_modules(path: Path) -> set[str]:
    """Every top-level module name imported anywhere in a file.

    Includes imports nested inside functions, which is where a shortcut would
    realistically be added -- Streamlit code defers imports for startup cost, so
    a module-level-only check would miss exactly the case worth catching.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def qualified_imports(path: Path) -> set[str]:
    """Dotted module paths, for asserting on a specific submodule."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.fixture(scope="module")
def app_imports() -> set[str]:
    return imported_modules(REPO / "app.py")


class TestStreamlitHoldsNoLogicOfItsOwn:
    """``app.py`` displays; it does not decide."""

    def test_it_never_touches_the_legacy_prototype_trees(self, app_imports: set[str]) -> None:
        leaked = app_imports & LEGACY_TREES
        assert not leaked, (
            f"app.py imports the prototype trees {sorted(leaked)} directly. The only "
            "permitted seam is `legacy_index` at the repository root."
        )

    def test_it_never_imports_a_retrieval_or_provider_library(self, app_imports: set[str]) -> None:
        leaked = app_imports & RETRIEVAL_STACK
        assert not leaked, (
            f"app.py imports {sorted(leaked)}. Retrieval belongs to synapse.retrieval, "
            "reached through SynapseService -- not to the fallback interface."
        )

    def test_it_never_runs_the_emergency_check_itself(self) -> None:
        """The one invariant that must not have two implementations.

        The emergency check precedes retrieval and generation inside
        ``synapse.service``. A copy in ``app.py`` could disagree with it, and
        the disagreement would be invisible until it mattered.
        """
        qualified = qualified_imports(REPO / "app.py")
        safety = {name for name in qualified if name.split(".")[:2] == ["synapse", "safety"]}
        assert not safety, (
            f"app.py imports the emergency detector directly ({sorted(safety)}). It must "
            "arrive through the service pipeline, so both interfaces escalate identically."
        )

    def test_it_answers_through_the_shared_service(self) -> None:
        qualified = qualified_imports(REPO / "app.py")
        assert "synapse.service" in qualified, (
            "app.py no longer imports synapse.service, so it is no longer the same "
            "application as the API."
        )
        # The call itself, not just the import: an import that nothing uses would
        # satisfy the assertion above while the turn came from somewhere else.
        source = (REPO / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        asks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ask"
        ]
        assert asks, "app.py never calls .ask(): the turn is not coming from the service"


class TestTheContainerDoesNotShipTheFallback:
    """The deployed image is FastAPI only."""

    def test_the_asgi_entrypoint_does_not_import_streamlit(self) -> None:
        assert "streamlit" not in imported_modules(REPO / "serve_api.py")

    def test_no_module_in_the_api_layer_imports_streamlit(self) -> None:
        offenders = [
            str(path.relative_to(REPO))
            for path in (REPO / "synapse" / "api").rglob("*.py")
            if "streamlit" in imported_modules(path)
        ]
        assert not offenders, f"the API layer imports streamlit: {offenders}"

    def test_the_service_layer_is_framework_neutral(self) -> None:
        """Neither web framework may reach the layer both interfaces share.

        This is what "framework-neutral" has to mean to be worth claiming: the
        turn pipeline cannot import Streamlit *or* FastAPI, so neither interface
        can quietly acquire behaviour the other does not have.
        """
        offenders: list[str] = []
        for path in (REPO / "synapse" / "service").rglob("*.py"):
            leaked = imported_modules(path) & {"streamlit", "fastapi"}
            if leaked:
                offenders.append(f"{path.relative_to(REPO)} -> {sorted(leaked)}")
        assert not offenders, f"synapse.service is not framework-neutral: {offenders}"


def _project() -> dict:
    with (REPO / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def _names(specs: list[str]) -> list[str]:
    """Distribution names, stripped of version specifiers and extras markers."""
    return [spec.split(">")[0].split("=")[0].split("[")[0].strip() for spec in specs]


class TestDependencyExtrasStaySeparate:
    """Installing the container must not install a second web framework."""

    def test_a_streamlit_extra_exists_so_the_fallback_is_installable(self) -> None:
        assert "streamlit" in _project()["optional-dependencies"], (
            "Streamlit is a supported fallback, so its dependencies must be declared "
            "as an extra rather than left to requirements.txt alone."
        )

    @pytest.mark.parametrize("extra", ["api", "runtime"])
    def test_the_container_extras_do_not_pull_in_streamlit(self, extra: str) -> None:
        names = _names(_project()["optional-dependencies"][extra])
        assert "streamlit" not in names, (
            f"the `{extra}` extra pulls in streamlit, so the deployed image would ship "
            "the fallback interface as well as the API."
        )

    def test_the_streamlit_extra_does_not_pull_in_the_api(self) -> None:
        names = _names(_project()["optional-dependencies"]["streamlit"])
        assert "fastapi" not in names, (
            "the fallback must not depend on the HTTP surface; it calls the service directly"
        )

    def test_the_core_package_depends_on_neither(self) -> None:
        names = _names(_project()["dependencies"])
        assert "streamlit" not in names
        assert "fastapi" not in names
