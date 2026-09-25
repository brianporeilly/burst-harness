# Burst Harness — a self-contained autonomous coding agent.
#
# Model credentials arrive by environment: ANTHROPIC_API_KEY, or
# HARNESS_TOKEN_COMMAND / HARNESS_TOKEN_FILE for a proxy, or whatever a
# provider plugin reads. Nothing is baked into the image.
FROM python:3.13-slim

# git and gh are load-bearing: gh is how finish_with_pr opens a pull request.
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

WORKDIR /opt/harness
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY harness/ ./harness/

# Snapshots and offloaded blobs. Mount a volume here to make resume survive the pod.
ENV HARNESS_STATE_DIR=/state \
    PYTHONPATH=/opt/harness \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /state /workspace && useradd -m -u 1000 agent \
 && chown -R agent:agent /state /workspace /opt/harness
USER agent
# Mounted repos are owned by the host uid, which trips git's dubious-ownership
# check. This image only ever works on repos it was explicitly pointed at.
RUN git config --global --add safe.directory '*'
WORKDIR /workspace

ENTRYPOINT ["python", "-m", "harness"]
CMD ["--help"]
