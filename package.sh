#!/usr/bin/env bash
#
# Build a clean, distributable zip of this project.
#
#   bash package.sh [output.zip]
#
# Invoke with `bash package.sh` rather than `./package.sh` so the script does
# not depend on its own executable bit surviving a file transfer.
#
# What the archive contains: the files git tracks in the current commit (HEAD),
# built with `git archive`: source, Dockerfiles, compose file, both lock files
# (ui/package-lock.json and api/requirements.txt) and the docs. That is
# everything needed to rebuild the stack from scratch. Untracked and ignored
# files never go in -- offline tars, .env files, node_modules/, previous
# archives, the contents of ingest-inbox/ and exports/ -- and neither do
# uncommitted changes, so commit first. Run it in a git checkout (#384).
#
# What it deliberately does NOT contain: Docker images, the Weaviate index, and
# the Ollama model weights. Those are rebuilt or re-downloaded on the target
# machine -- roughly 8-10 GB of network traffic on first run.
#
# THE TARGET MACHINE MUST HAVE INTERNET ACCESS for this package to work.
# For an air-gapped install, use package-offline.sh instead: it ships the
# pre-built images and model weights, and needs no network on the target.
# See "Moving to another Mac" in README.md.

set -euo pipefail

cd "$(dirname "$0")"

# git archive works on the repository, so this directory must be the top level
# of a git checkout. In an extracted archive there is no repository, and in a
# subdirectory of another one git archive would take only that subtree.
TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || TOP=""
if [ -z "$TOP" ] || [ "$(cd "$TOP" && pwd -P)" != "$(pwd -P)" ]; then
  echo "ERROR: $(pwd) is not the top level of a git checkout. package.sh archives the tracked files of the current commit with git archive; run it in a clone of the repository." >&2
  exit 1
fi

OUT="${1:-rag-docker.zip}"
rm -f "$OUT"

# Tracked files only. ingest-inbox/.gitkeep and exports/.gitkeep are tracked, so
# both bind-mount targets exist on the target machine; without them Docker
# creates the directories as root and the user cannot drop files in.
git archive --format=zip -o "$OUT" HEAD

echo "Created $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "On the target Mac:"
echo "  unzip $OUT -d rag-docker && cd rag-docker"
echo "  docker compose up -d"
echo
echo "First run pulls base images, builds both services and downloads ~2.5 GB of"
echo "model weights, so the target machine needs internet access. Allow 20 GB of"
echo "Docker disk (32 GB recommended) -- see README.md."
echo
echo "No internet on the target? Use: bash package-offline.sh"
