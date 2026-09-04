# The FastAPI container. One process, no database, no Streamlit.
#
# What is deliberately NOT here
# -----------------------------
# * **Streamlit.** The fallback interface is a local convenience over the same
#   `synapse.service`; the deployed image installs `.[api,runtime]` and nothing
#   else, so a second web framework cannot reach production.
#   `tests/test_streamlit_fallback.py` asserts those extras stay disjoint.
# * **The legacy prototype and its pickle corpus.** Excluded by `.dockerignore`.
#   The image serves from the `synapse` package alone.
# * **Secrets.** No ARG, no ENV and no COPY carries a credential. Everything is
#   read from the environment at request time. `.env` is in `.dockerignore`.
#
# Two stages so the compiler toolchain used to install dependencies is not
# present in the shipped image: a build container that can compile is a build
# container an attacker can compile in.

# --- Stage 1: build the virtualenv -----------------------------------------
FROM python:3.12-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Only the metadata first, so a source-only change does not re-resolve and
# re-download every dependency.
COPY pyproject.toml README.md ./
COPY synapse/__init__.py synapse/__init__.py

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# `.[api,runtime]` — the HTTP surface plus the verified-artifact retrieval
# stack. NOT `.[streamlit]`, and not `.[dev]`.
RUN pip install --no-cache-dir ".[api,runtime]"

# --- Stage 2: the runtime image --------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root. The application reads a pinned artifact and writes nothing, so it
# has no reason to own any path it executes from.
RUN groupadd --system --gid 10001 synapse \
 && useradd --system --uid 10001 --gid synapse --no-create-home --shell /usr/sbin/nologin synapse

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root synapse/ synapse/
COPY --chown=root:root serve_api.py legacy_index.py pyproject.toml README.md ./

# NOTE: the emergency vocabulary needs no COPY of its own. It lives inside the
# package (`synapse/safety/emergency_vocabulary.toml`) and is declared as
# package data, so it arrives with `synapse/` above and with the installed wheel
# in the build stage. It previously sat at the repository's `config/`, resolved
# relative to the source tree, and an image built without an explicit COPY
# reported itself permanently unready with VocabularyError.

# The artifact cache, created and owned by the user that has to write to it.
#
# Render mounts a persistent disk here; this is the path
# `synapse.runtime.config.DEFAULT_CACHE_ROOT` resolves to. Without it the
# process runs as uid 10001 against a root-owned path and startup fails with
# `index_unverified` / `PermissionError` — an artifact it could have downloaded,
# refused because it had nowhere to put it. Creating it in the image also means
# a deployment with no disk attached still works, using container-local storage
# that is simply lost on restart.
RUN mkdir -p /var/data/synapse && chown -R synapse:synapse /var/data
VOLUME ["/var/data/synapse"]

USER synapse:synapse

EXPOSE 8000

# Liveness only — deliberately NOT /readyz. Readiness failing means "do not send
# traffic"; wiring it to the container health check would turn a diagnosable
# unready container into a crash loop, which is the conflation
# `synapse/api/routes/health.py` exists to avoid.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# `--proxy-headers` because the process sits behind the platform's TLS
# terminator; without it every request appears to come from the proxy.
CMD ["python", "-m", "uvicorn", "serve_api:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
