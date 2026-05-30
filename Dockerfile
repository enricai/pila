# pila container image — see docs/IMPLEMENTATION.md §0.5 "Container shape".
#
# Built locally on first `pila` run; tagged `pila:<VERSION>` so a pila upgrade
# rebuilds once and reuses layers thereafter.
#
# LOCAL MODE: the launcher bind-mounts $PILA_HOME → /work/.pila-image:ro at
# runtime, shadowing the baked-in COPY layers. Editing orchestrator/pila.py
# on the host takes effect on the next run without an image rebuild.
#
# REGISTRY / FLY MODE: the COPY instructions below bake orchestrator/,
# scripts/, prompts/, and .claude-plugin/ into /work/.pila-image/ so the
# image is self-contained without a bind-mount. An image rebuild IS required
# after source changes.

FROM debian:12-slim

# Base tools pila + claude -p + typical worker tasks need.
# build-essential covers native-module compilation (sharp, bcrypt, etc.) so
# `npm install` doesn't fail on first run in a fresh worktree.
# procps provides `ps`, which the orchestrator's PPID-walk fast-cleanup path
# (pila.py:925) calls between waves. Without it the walk silently degrades
# to no-op via the OSError catch — correctness is fine, but the documented
# fast-happy-path is gone. ~1MB image cost.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client \
      python3 python3-pip \
      procps \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — the finalize phase pushes the run branch and opens a PR via
# `gh pr create` (pila.py:5828). Without this, default-mode runs die at the
# preflight `shutil.which("gh")` check (pila.py:1282). GitHub publishes a
# Debian apt repo with arch-aware packages; install from there.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# mise — polyglot version manager (formerly rtx). Owns the per-repo
# runtime version selection (DESIGN §6½). Reads .tool-versions natively
# and .nvmrc / .python-version / .ruby-version / rust-toolchain.toml
# via the MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS env below (those
# files are opt-in in current mise; without the env they are silently
# ignored). go.mod's `go 1.X` line is NOT parsed by mise; pila
# synthesizes a .go-version override at provision time.
#
# The mise.run script reads $MISE_VERSION; pin for reproducibility.
ARG MISE_VERSION=v2026.5.4
RUN curl -fsSL https://mise.run | MISE_VERSION="${MISE_VERSION}" sh \
    && mv /root/.local/bin/mise /usr/local/bin/mise \
    && rm -rf /root/.local /root/.config /root/.cache

# mise env directives. ENV survives across RUNs and into the runtime
# container (a shell `export` inside a RUN would not).
#
# IDIOMATIC_VERSION_FILE_ENABLE_TOOLS is load-bearing: mise treats
# .nvmrc / .python-version / .ruby-version / rust-toolchain.toml as
# "idiomatic" files that are disabled by default. Without this flag,
# a repo with `.nvmrc: 20.11.0` would silently run on baked LTS.
#
# NODE_COREPACK activates corepack so package.json's `packageManager`
# field is honored — repo's pinned pnpm version wins, no global pin
# needed.
#
# DATA_DIR is where mise installs per-repo runtime versions; the
# launcher bind-mounts this from ~/.cache/pila/mise-data so installs
# survive across runs. SYSTEM_DATA_DIR is where the LTS fallback
# lives (baked below). The resolver checks DATA_DIR first then falls
# through to SYSTEM_DATA_DIR (mise.jdx.dev/mise-cookbook/docker.html).
ENV MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS=node,python,ruby,rust
ENV MISE_NODE_COREPACK=true
ENV MISE_DATA_DIR=/home/pila/.local/share/mise
ENV MISE_SYSTEM_DATA_DIR=/usr/local/share/mise

# Pre-install LTS Node + Python via `mise install --system`. Lands
# binaries under /usr/local/share/mise/installs/<tool>/<version>.
# At runtime mise's resolver falls through to these from the user
# dir if a repo declares no version. ~150-200 MB image cost.
RUN mise install --system node@lts python@3.12

# Stable PATH symlink for the LTS Node bin. `mise install --system
# node@lts` resolves to a concrete version directory under
# /usr/local/share/mise/installs/node/<version>/ — we symlink it to a
# stable name so PATH and the claude global-install below don't have
# to know the concrete version. The wildcard expansion is safe
# because exactly one node version is installed in this layer.
RUN set -eux; \
    node_dir="$(ls -d /usr/local/share/mise/installs/node/*/ | head -n1)"; \
    ln -s "${node_dir%/}" /usr/local/share/mise/installs/node/lts-current

# PATH order:
#   1. mise's system shims (mise install --system populates these).
#   2. LTS Node bin (image-baked claude lives here).
#   3. (then the pre-existing PATH)
# At runtime the user dir's shims at /home/pila/.local/share/mise/shims
# don't appear here because they're added by `mise activate` or by
# wrapping commands with `mise exec --` — both of which the
# orchestrator does explicitly when invoking install commands.
ENV PATH=/usr/local/share/mise/shims:/usr/local/share/mise/installs/node/lts-current/bin:$PATH

# Claude Code CLI. Pila enforces ≥ 2.1.22 at runtime (pila.py:1245).
# Installs globally against the LTS Node — lands at
# /usr/local/share/mise/installs/node/lts-current/lib/node_modules
# with a bin shim at .../bin/claude (on PATH via the line above).
RUN npm install -g @anthropic-ai/claude-code

# Non-root user matching the host UID/GID so bind-mounted files keep their
# host ownership. Defaults are macOS-typical; the launcher overrides them
# via --build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g).
ARG HOST_UID=501
ARG HOST_GID=20
RUN if ! getent group "${HOST_GID}" >/dev/null 2>&1; then \
      groupadd -g "${HOST_GID}" pila; \
    fi; \
    useradd -u "${HOST_UID}" -g "${HOST_GID}" -m -s /bin/bash pila

# /inspect/ holds read-only bind mounts the launcher creates per
# --inspect-dir flag. Pre-created (and owned by pila) so the mount targets
# exist when nerdctl creates them at runtime.
RUN mkdir -p /inspect && chown pila:"${HOST_GID}" /inspect

# Pre-create the pila user's MISE_DATA_DIR and the per-tool cache
# mount targets so the launcher's bind-mounts attach cleanly and the
# user dir owns the right metadata for mise's first run. Also chown
# /home/pila itself — `useradd -m` produces /home/pila with mode 0755
# but observed images have it as root:root, so the runtime user can't
# create new dotfiles (e.g. `mise install` invokes gpg, which mkdirs
# ~/.gnupg and fails with EACCES). The .gnupg subdir is pre-created
# at mode 0700, which GPG requires.
RUN mkdir -p /home/pila/.local/share/mise \
             /home/pila/.cache/pila/pnpm-store \
             /home/pila/.cache/pila/pip \
             /home/pila/.cache/pila/go-mod \
             /home/pila/.cache/pila/cargo \
             /home/pila/.gnupg \
    && chown pila:"${HOST_GID}" /home/pila \
    && chown -R pila:"${HOST_GID}" /home/pila/.local /home/pila/.cache /home/pila/.gnupg \
    && chmod 700 /home/pila/.gnupg

# Bake the orchestrator source into the image at /work/.pila-image/ so the
# image is self-contained on Fly.io Machines (no host bind mount available).
# On local runs the launcher's `-v $PILA_REPO:/work/.pila-image:ro` shadows
# this baked copy, so development iteration (edit + run) works without
# rebuilding the image. COPY runs as root; chown transfers ownership to pila.
COPY orchestrator/ /work/.pila-image/orchestrator/
COPY scripts/ /work/.pila-image/scripts/
COPY prompts/ /work/.pila-image/prompts/
COPY .claude-plugin/ /work/.pila-image/.claude-plugin/
RUN chown -R pila:"${HOST_GID}" /work/.pila-image

USER pila
WORKDIR /work

ENTRYPOINT ["/work/.pila-image/scripts/container-entry.sh"]
