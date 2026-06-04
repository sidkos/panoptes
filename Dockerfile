# Panoptes collector + MCP runtime image.
#
# Replaces the prior compose-inline workaround (A8: stock `python:3.12-slim` +
# a bind-mounted `:ro` source + a runtime `pip install` into /tmp). That hack
# existed only because an in-place `pip install .` over a READ-ONLY source tree
# cannot write `panoptes.egg-info`. Building the install at BUILD time sidesteps
# that entirely — the build context is writable, so setuptools' egg_info step
# succeeds — and bakes the dependency wheels into the image (no runtime pip, so
# the services start fast and work offline).
#
# Layer order is deliberate for cache hits:
#   1. deps layer (pyproject + README) — invalidated only when deps change.
#   2. source layer (core/) — invalidated on every core/ edit, but the heavy
#      dependency install above it stays cached.
#
# examples/ is intentionally NOT copied: the consumer/demo pack is injected at
# runtime via the compose `:ro` mount at /packs/consumer, preserving the
# core/consumer boundary. `.dockerignore` also excludes examples/ so setuptools
# `packages.find` resolves only `core*` inside the build context.
#
# No default CMD — the `collector` and `mcp` compose services each set their own
# `command:` (the two entrypoints share this one image).
FROM python:3.12-slim
WORKDIR /app

# Dependency layer first: copy only the install metadata so this expensive layer
# is cached until pyproject.toml (or the README it references) actually changes.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Source last: a plain edit under core/ invalidates only this cheap final layer,
# not the dependency install above.
COPY core ./core

# Run as a non-root user (numeric UID 1000) so the Helm chart's `runAsNonRoot: true` pod
# securityContext is satisfied — the kubelet REJECTS a root-running image when runAsNonRoot is
# set ("container has runAsNonRoot and image will run as root"), which a real cluster deploy
# surfaces even though `helm template` cannot. A numeric UID lets the kubelet verify non-root
# without a passwd lookup; the baked install under /usr/local is root-owned but only READ at
# runtime, so a non-root user + the chart's readOnlyRootFilesystem both work. PYTHONDONTWRITEBYTECODE
# stops Python writing .pyc into the read-only root filesystem.
ENV PYTHONDONTWRITEBYTECODE=1
USER 1000
