# Burst Harness — a self-contained autonomous coding agent.
#
# Model credentials arrive by environment: ANTHROPIC_API_KEY, or
# HARNESS_TOKEN_COMMAND / HARNESS_TOKEN_FILE for a proxy, or whatever a
# provider plugin reads. Nothing is baked into the image.
FROM python:3.13-slim

# git and gh are load-bearing: git for local-target work, gh because
# finish_with_pr opens pull requests from the driver.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# kubectl drives the kubectl and agent-sandbox targets (claims, exec). Pinned to
# the cluster's minor version and checksum-verified; in a pod it uses the
# in-cluster ServiceAccount with no kubeconfig.
ARG KUBECTL_VERSION=v1.36.4
RUN arch="$(dpkg --print-architecture)" \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl.sha256")  /usr/local/bin/kubectl" \
      | sha256sum -c - \
 && chmod 755 /usr/local/bin/kubectl

# Dependencies from uv.lock, exactly, into /opt/harness/.venv: first on their own,
# so a source change does not reinstall them, then the project itself, editable,
# so the code stays at /opt/harness/harness as the orchestrator expects (jobs.REPO).
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /usr/local/bin/uv
WORKDIR /opt/harness
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/harness/.venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra openai --no-cache
COPY harness/ ./harness/
# The orchestrator, for a Deployment: `python -m orchestrator` in place of the entrypoint.
COPY orchestrator/ ./orchestrator/
RUN uv sync --frozen --no-dev --extra openai --no-cache

# Snapshots and offloaded blobs. Mount a volume here to make resume survive the pod.
# The venv's python first, so `python -m harness` and `python -m orchestrator` are its.
ENV HARNESS_STATE_DIR=/state \
    PATH=/opt/harness/.venv/bin:$PATH \
    VIRTUAL_ENV=/opt/harness/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The commit this was built from, for the orchestrator's /version (images.yml sets it). Last, so a new
# commit rebuilds nothing above.
ARG REVISION=""
ENV BURST_HARNESS_REVISION=$REVISION
RUN mkdir -p /state /workspace && useradd -m -u 1000 agent \
 && chown -R agent:agent /state /workspace /opt/harness
USER agent
# Mounted repos are owned by the host uid, which trips git's dubious-ownership
# check. This image only ever works on repos it was explicitly pointed at.
RUN git config --global --add safe.directory '*'
WORKDIR /workspace

ENTRYPOINT ["python", "-m", "harness"]
CMD ["--help"]
