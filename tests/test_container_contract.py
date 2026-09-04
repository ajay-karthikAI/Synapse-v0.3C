"""
What the deployed image must and must not contain.

These run offline and without Docker, so the properties are checked on every
push rather than only in the job that can build an image. The container smoke
test (``scripts/smoke_container.sh``) proves the same things against a running
container; this file makes a regression visible in the fast suite first.

The assertions that earned this module
--------------------------------------
``load_detector`` used to resolve the emergency vocabulary **relative to the
repository root** -- ``config/emergency_vocabulary.toml``, two levels above
``synapse/safety/vocabulary.py``. That works in a source checkout and nowhere
else: from an installed wheel the same expression points at
``site-packages/config/``, which does not exist. The first image built from this
package started, passed its liveness check, and reported itself permanently
unready with ``detail="VocabularyError"`` -- failing closed, correctly, but
unable to answer anything. Since the emergency check precedes retrieval and
generation, a package that cannot find this file cannot run the first safety
gate at all.

The file now lives **inside** the package and is declared as package data, so it
travels with the code however the code was installed. These tests hold that
property from three directions: the resolved path is inside the package, the
file is present in a freshly built wheel, and the detector actually loads.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from synapse.safety.vocabulary import DEFAULT_VOCABULARY_PATH

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"


def dockerfile_lines() -> list[str]:
    """Instruction lines only, with comments and blanks removed.

    Comment-stripped for the reason this repository keeps rediscovering: the
    Dockerfile documents what it excludes, so a naive text search finds the
    sentence explaining the rule and reports it as the rule being broken.
    """
    lines = []
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


class TestTheEmergencyVocabularyShips:
    """The first safety gate needs a data file that lives outside the package."""

    def test_the_default_path_exists_in_the_repository(self) -> None:
        assert DEFAULT_VOCABULARY_PATH.exists(), (
            f"{DEFAULT_VOCABULARY_PATH} is missing; the emergency detector cannot load."
        )

    def test_the_default_path_is_inside_the_installable_package(self) -> None:
        """The property that makes the wheel, the container and a checkout agree."""
        package_root = REPO / "synapse"
        assert package_root in DEFAULT_VOCABULARY_PATH.parents, (
            f"{DEFAULT_VOCABULARY_PATH} is outside {package_root}. Resolved relative to "
            "the source tree, it cannot be found from an installed wheel, and the "
            "emergency detector raises on load."
        )

    def test_it_is_declared_as_package_data(self) -> None:
        with (REPO / "pyproject.toml").open("rb") as handle:
            package_data = tomllib.load(handle)["tool"]["setuptools"]["package-data"]
        relative = DEFAULT_VOCABULARY_PATH.relative_to(REPO / "synapse").as_posix()
        patterns = package_data.get("synapse", [])
        assert any(relative == pattern or pattern.endswith(".toml") for pattern in patterns), (
            f"{relative} is not covered by [tool.setuptools.package-data]; setuptools "
            "ships .py files only, so it would be absent from the wheel."
        )

    def test_it_is_present_in_a_freshly_built_wheel(self) -> None:
        """The end of the argument: build the artifact and look inside it.

        Slower than the checks above and worth it -- those assert intent, this
        asserts the thing that actually gets deployed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", tmp, "-q"],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                pytest.skip(f"could not build a wheel here: {result.stderr.strip()[:200]}")
            wheels = list(Path(tmp).glob("*.whl"))
            assert wheels, "pip wheel produced no wheel"
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
        expected = DEFAULT_VOCABULARY_PATH.relative_to(REPO).as_posix()
        assert expected in names, (
            f"{expected} is missing from the wheel. An install from it cannot run the "
            f"emergency check. Wheel contains: {sorted(n for n in names if 'safety' in n)}"
        )

    def test_the_loader_actually_loads_it(self) -> None:
        from synapse.safety import load_detector

        detector = load_detector()
        assert detector is not None

    def test_an_operator_can_override_it_without_rebuilding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A reviewed vocabulary can be mounted over the shipped one."""
        from synapse.safety.vocabulary import default_vocabulary_path

        target = tmp_path / "reviewed.toml"
        target.write_text("", encoding="utf-8")
        monkeypatch.setenv("SYNAPSE_EMERGENCY_VOCABULARY", str(target))
        assert default_vocabulary_path() == target


class TestTheImageExcludesWhatItMustNot:
    """A layer is readable by anyone who can pull the image."""

    @pytest.mark.parametrize("pattern", [".env", "*.pkl"])
    def test_dockerignore_excludes_secrets_and_pickles(self, pattern: str) -> None:
        entries = {
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        assert pattern in entries, f".dockerignore does not exclude {pattern}"

    def test_the_image_does_not_install_the_streamlit_extra(self) -> None:
        installs = [line for line in dockerfile_lines() if "pip install" in line]
        assert installs, "the Dockerfile installs nothing"
        for line in installs:
            assert "streamlit" not in line, (
                f"the image installs the Streamlit extra: {line}. The container is the "
                "API only; the fallback interface is local."
            )

    def test_it_runs_as_a_non_root_user(self) -> None:
        users = [line for line in dockerfile_lines() if line.startswith("USER ")]
        assert users, "the Dockerfile never drops from root"
        assert not users[-1].split()[1].startswith("root"), f"final USER is root: {users[-1]}"

    def test_no_credential_is_baked_into_a_layer(self) -> None:
        """No ARG or ENV may carry something shaped like a secret."""
        suspicious = re.compile(r"\b(ARG|ENV)\b.*\b\w*(SECRET|TOKEN|PASSWORD|API_KEY)\w*\s*=")
        offenders = [line for line in dockerfile_lines() if suspicious.search(line)]
        assert not offenders, f"the Dockerfile bakes in a credential: {offenders}"

    def test_the_healthcheck_uses_liveness_not_readiness(self) -> None:
        """Wiring the health check to /readyz turns unready into a crash loop.

        That conflation is precisely what `synapse/api/routes/health.py` splits
        the two endpoints to prevent, so it is asserted rather than trusted.
        """
        checks = [line for line in dockerfile_lines() if line.startswith("HEALTHCHECK")]
        # The instruction continues onto the CMD line, so search the raw text of
        # the whole instruction block instead of the first line alone.
        text = DOCKERFILE.read_text(encoding="utf-8")
        block = text[text.index("HEALTHCHECK") :] if checks else ""
        block = block[: block.index("\nCMD")] if "\nCMD" in block else block
        assert "/healthz" in block, "the container health check does not probe /healthz"
        assert "/readyz" not in block, (
            "the container health check probes /readyz; an unready container would be "
            "killed and restarted instead of reporting why it cannot serve."
        )
