#!/bin/sh
# container-entry.sh — PID 1 of the pila container.
#
# Bind-mounted from $PILA_HOME/scripts/container-entry.sh on the host to
# /work/.pila-image/scripts/container-entry.sh inside the container, and
# referenced by Dockerfile's ENTRYPOINT.
#
# All it does: cd into the user's repo (bind-mounted at /work) and exec the
# orchestrator. PID 1 in a container is what the kernel reaps the namespace
# under when it exits — see docs/DESIGN.md §6 and docs/IMPLEMENTATION.md §0.5.
set -e
# Suppress core dumps from OOM-killed workers — on large codebases
# (e.g. Next.js apps with heavy tsc + bundler memory use), `next build`
# or vitest can be OOM-killed inside Colima and otherwise leave
# multi-GB core files behind in each per-subtask worktree. Setting
# RLIMIT_CORE=0 at PID 1 is inherited by every worker subprocess.
ulimit -c 0
cd /work
exec python3 /work/.pila-image/orchestrator/pila.py "$@"
