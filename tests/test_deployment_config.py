"""
The names an operator types must be the names the code reads.

The hosting configuration and the application were written against different
vocabularies: Render and Vercel set ``DEMO_ACCESS_PASSWORD``,
``DEMO_SESSION_SECRET``, ``SYNAPSE_ARTIFACT_S3_ENDPOINT`` and
``SYNAPSE_ENVIRONMENT``; the code reads ``SYNAPSE_ACCESS_PASSCODE``,
``SYNAPSE_JWT_SECRET``, ``SYNAPSE_ARTIFACT_ENDPOINT_URL`` and ``SYNAPSE_ENV``.

Every one of those mismatches fails **silently and misleadingly**:

* a missing passcode does not say "unconfigured", it says "wrong passcode";
* a missing JWT secret does not say "unconfigured", it says "your session has
  expired" -- on every request, forever;
* a missing artifact endpoint does not say "unconfigured", it tries the public
  AWS endpoint for a bucket that is not there;
* an unparsed environment silently labels demo traffic ``local``.

So the aliases are asserted here, and `render.yaml` / `vercel.json` are checked
against the same list. A rename on either side fails the build rather than the
deployment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from synapse.api.config import (
    ENV_JWT_SECRET,
    ENV_JWT_SECRET_ALIAS,
    ENV_PASSCODE,
    ENV_PASSCODE_ALIAS,
    ENV_SERVICE_TOKEN,
    ApiConfigurationError,
    ApiSettings,
)
from synapse.runtime.config import ENV_ENDPOINT, ENV_ENDPOINT_ALIAS, RuntimeArtifactConfig
from synapse.telemetry.config import TelemetryConfig
from synapse.telemetry.schema import Environment

REPO = Path(__file__).resolve().parent.parent

# Every variable the deployment sets, and nothing it does not. Kept as one list
# so the runbook, render.yaml and this file can be checked against each other.
RENDER_SECRETS = {
    "OPENAI_API_KEY",
    "SYNAPSE_SERVICE_TOKEN",
    "DEMO_ACCESS_PASSWORD",
    "DEMO_SESSION_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
}
RENDER_ARTIFACT_VARS = {
    "SYNAPSE_ARTIFACT_S3_ENDPOINT",
    "SYNAPSE_ARTIFACT_BUCKET",
    "SYNAPSE_ARTIFACT_KEY",
    "SYNAPSE_ARTIFACT_SHA256",
    # Required by RuntimeArtifactConfig.validate, and omitted from the first
    # draft of render.yaml. A container without it reported
    # `configuration_error` and could never load an artifact -- which is the
    # correct behaviour and a deployment that silently never works.
    "SYNAPSE_ARTIFACT_VERSION",
}
VERCEL_SERVER_VARS = {"SYNAPSE_API_URL", "SYNAPSE_SERVICE_TOKEN", "DEMO_SESSION_SECRET"}


class TestTheDeploymentAliasesResolve:
    """A value set under the deployment name reaches the code."""

    def test_the_passcode_alias_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_PASSCODE, raising=False)
        monkeypatch.setenv(ENV_PASSCODE_ALIAS, "a-demo-passcode")
        assert ApiSettings.from_environment().passcode == "a-demo-passcode"

    def test_the_session_secret_alias_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_JWT_SECRET, raising=False)
        monkeypatch.setenv(ENV_JWT_SECRET_ALIAS, "x" * 32)
        assert ApiSettings.from_environment().jwt_secret == "x" * 32

    @pytest.mark.parametrize(
        ("primary", "alias"),
        [(ENV_PASSCODE, ENV_PASSCODE_ALIAS), (ENV_JWT_SECRET, ENV_JWT_SECRET_ALIAS)],
    )
    def test_the_primary_name_still_wins(
        self, monkeypatch: pytest.MonkeyPatch, primary: str, alias: str
    ) -> None:
        """An existing local .env must not be overridden by a stray alias."""
        monkeypatch.setenv(primary, "primary-value-0123456789abcdef")
        monkeypatch.setenv(alias, "alias-value-0123456789abcdef")
        settings = ApiSettings.from_environment()
        chosen = settings.passcode if primary == ENV_PASSCODE else settings.jwt_secret
        assert chosen == "primary-value-0123456789abcdef"

    def test_the_artifact_endpoint_alias_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_ENDPOINT, raising=False)
        monkeypatch.setenv(ENV_ENDPOINT_ALIAS, "https://example.r2.cloudflarestorage.com")
        monkeypatch.setenv("SYNAPSE_ARTIFACT_BUCKET", "synapse-artifacts")
        monkeypatch.setenv("SYNAPSE_ARTIFACT_KEY", "runtime/v1.tar.gz")
        monkeypatch.setenv("SYNAPSE_ARTIFACT_SHA256", "a" * 64)
        config = RuntimeArtifactConfig.from_environment()
        assert config.endpoint_url == "https://example.r2.cloudflarestorage.com"

    def test_the_environment_alias_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SYNAPSE_ENV", raising=False)
        monkeypatch.setenv("SYNAPSE_ENVIRONMENT", "demo")
        assert TelemetryConfig.from_environment().environment is Environment.DEMO

    def test_demo_is_not_production(self) -> None:
        """An aggregate that merged them would misrepresent what this is."""
        assert Environment.DEMO is not Environment.PRODUCTION
        assert Environment.DEMO.value == "demo"

    def test_a_missing_secret_names_both_accepted_variables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in (ENV_PASSCODE, ENV_PASSCODE_ALIAS, ENV_JWT_SECRET, ENV_JWT_SECRET_ALIAS):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(ENV_SERVICE_TOKEN, "t" * 32)
        with pytest.raises(ApiConfigurationError) as caught:
            ApiSettings.from_environment().validate()
        message = str(caught.value)
        assert ENV_PASSCODE_ALIAS in message and ENV_JWT_SECRET_ALIAS in message, (
            f"the error names only one of the accepted variables: {message}"
        )


def _render() -> dict:
    """render.yaml, parsed.

    A plain function rather than a class-scoped fixture, which pytest mishandles
    alongside parametrisation in this file.

    `yaml` is imported at module scope and `pyyaml` is a declared dev
    dependency, deliberately: an `importorskip` here would silently skip every
    assertion about the deployment blueprint on any machine that happened not to
    have it, and a blueprint nobody checks is worse than no blueprint.
    """
    return yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))


class TestRenderConfiguration:
    """render.yaml says what the runbook says."""

    def test_one_always_on_web_service_with_one_worker(self) -> None:
        render = _render()
        services = render["services"]
        assert len(services) == 1, "one always-on service, not a fleet"
        service = services[0]
        assert service["type"] == "web"
        # One worker: sessions are IN-MEMORY, so a second worker would answer
        # half the requests from a process that has never seen the session.
        joined = " ".join(str(v) for v in service.values())
        assert "--workers 1" in joined or "workers=1" in joined

    def test_health_check_uses_readyz(self) -> None:
        render = _render()
        assert render["services"][0]["healthCheckPath"] == "/readyz"

    def test_the_persistent_disk_is_the_artifact_cache(self) -> None:
        render = _render()
        disk = render["services"][0]["disk"]
        assert disk["mountPath"] == "/var/data/synapse"

    def test_every_secret_is_declared_without_a_value(self) -> None:
        """`sync: false` means Render prompts; the value is never in the repo."""
        render = _render()
        declared = {entry["key"]: entry for entry in render["services"][0]["envVars"]}
        for secret in RENDER_SECRETS:
            assert secret in declared, f"{secret} is not declared in render.yaml"
            assert "value" not in declared[secret], f"{secret} has a literal value in the repo"
            assert declared[secret].get("sync") is False, f"{secret} must be sync: false"

    def test_artifact_configuration_is_present(self) -> None:
        render = _render()
        declared = {entry["key"] for entry in render["services"][0]["envVars"]}
        assert declared >= RENDER_ARTIFACT_VARS, (
            f"missing artifact configuration: {sorted(RENDER_ARTIFACT_VARS - declared)}"
        )

    def test_it_declares_every_variable_the_artifact_loader_requires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Derived from the loader, not from a list someone maintained.

        `render.yaml` originally declared four artifact variables where
        `RuntimeArtifactConfig.validate` requires five. The container started,
        reported `configuration_error`, and could never load anything. Asking
        the validator itself which names it wants means the blueprint cannot
        drift from the code again.
        """
        declared = {entry["key"] for entry in _render()["services"][0]["envVars"]}
        for name in declared:
            monkeypatch.delenv(name, raising=False)

        # Populate exactly what the blueprint declares, then confirm the loader
        # is satisfied. A name the loader wants and the blueprint omits shows up
        # here as a missing-value error naming it.
        for name in declared:
            monkeypatch.setenv(name, "a" * 64 if name.endswith("SHA256") else "x")
        RuntimeArtifactConfig.from_environment().validate()

    def test_the_environment_is_demo(self) -> None:
        render = _render()
        declared = {entry["key"]: entry.get("value") for entry in render["services"][0]["envVars"]}
        assert declared.get("SYNAPSE_ENVIRONMENT") == "demo"

    def test_no_secret_value_is_committed(self) -> None:
        raw = (REPO / "render.yaml").read_text(encoding="utf-8")
        assert "sk-" not in raw, "an API key literal is in render.yaml"


class TestVercelConfiguration:
    """The browser bundle cannot receive a secret."""

    # Vercel reads `vercel.json` from the project's Root Directory, which is
    # `frontend/`. A copy at the repository root would be ignored.
    VERCEL_JSON = REPO / "frontend" / "vercel.json"

    def test_no_next_public_variable_carries_a_secret(self) -> None:
        """The single worst possible outcome, asserted across the whole repo.

        `NEXT_PUBLIC_` is inlined into the client bundle at build time. A
        service token there is a public backend.
        """
        offenders: list[str] = []
        for path in (REPO / "frontend" / "src").rglob("*.ts*"):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "NEXT_PUBLIC_" in line and not line.strip().startswith(("*", "//", "#")):
                    offenders.append(f"{path.relative_to(REPO)}: {line.strip()[:80]}")
        assert not offenders, f"NEXT_PUBLIC_ used in application code: {offenders}"

    def test_vercel_declares_no_env_values_at_all(self) -> None:
        vercel = json.loads(self.VERCEL_JSON.read_text(encoding="utf-8"))
        """No secret, and no `env` block either.

        A value in `vercel.json` is committed to the repository and inlined at
        build time. Every one of the three server-side variables is set in the
        Vercel dashboard per environment instead, which is also what makes
        separate preview and production values possible.
        """
        assert "env" not in vercel, "vercel.json declares env values; set them in the dashboard"
        assert "build" not in vercel or "env" not in vercel.get("build", {})
        raw = json.dumps(vercel)
        for marker in ("sk-", "SYNAPSE_SERVICE_TOKEN", "DEMO_SESSION_SECRET"):
            assert marker not in raw, f"vercel.json appears to contain a secret: {marker}"

    def test_it_lives_in_the_project_root_directory(self) -> None:
        assert self.VERCEL_JSON.is_file(), (
            "Vercel reads vercel.json from the Root Directory (frontend/). A copy at the "
            "repository root is ignored."
        )
        assert not (REPO / "vercel.json").exists(), (
            "a second vercel.json at the repository root will be ignored and will drift"
        )

    def test_it_does_not_duplicate_headers_next_config_already_sets(self) -> None:
        vercel = json.loads(self.VERCEL_JSON.read_text(encoding="utf-8"))
        """One owner for security headers.

        `next.config.ts` sets X-Frame-Options, nosniff, Referrer-Policy,
        Permissions-Policy and HSTS. Declaring them here as well creates two
        places to change and one to forget.
        """
        assert "headers" not in vercel, (
            "security headers belong to next.config.ts, which already sets them"
        )

    def test_the_server_only_variables_are_documented(self) -> None:
        runbook = (REPO / "docs" / "deployment.md").read_text(encoding="utf-8")
        for name in VERCEL_SERVER_VARS:
            assert name in runbook, f"{name} is not in the deployment runbook"


class TestNoBrowserAnalyticsOrErrorReporting:
    """No third-party script may observe a patient reading a medical result."""

    FORBIDDEN = (
        "@sentry",
        "sentry.io",
        "@vercel/analytics",
        "@vercel/speed-insights",
        "googletagmanager",
        "google-analytics",
        "posthog",
        "mixpanel",
        "datadoghq",
        "segment.com",
        "hotjar",
        "fullstory",
        "logrocket",
    )

    def test_no_analytics_dependency_is_installed(self) -> None:
        manifest = json.loads((REPO / "frontend" / "package.json").read_text(encoding="utf-8"))
        installed = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
        offenders = [
            name for name in installed if any(bad in name.lower() for bad in self.FORBIDDEN)
        ]
        assert not offenders, f"browser analytics or error reporting installed: {offenders}"

    def test_no_analytics_host_is_referenced_in_application_code(self) -> None:
        offenders: list[str] = []
        for path in (REPO / "frontend" / "src").rglob("*.ts*"):
            lowered = path.read_text(encoding="utf-8").lower()
            for bad in self.FORBIDDEN:
                # Comment lines are stripped first: this repository documents
                # what it refuses to load, and the sentence explaining the rule
                # must not read as the rule being broken.
                for line in lowered.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("*", "//", "/*", "#")):
                        continue
                    if bad in stripped:
                        offenders.append(f"{path.relative_to(REPO)}: {bad}")
        assert not offenders, f"analytics host referenced: {sorted(set(offenders))}"
