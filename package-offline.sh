#!/usr/bin/env bash
#
# Build a fully self-contained OFFLINE bundle of this project.
#
#   bash package-offline.sh [output.tar]
#
# Unlike package.sh (source only, ~1.3 MB, needs the internet on first run),
# this bundle contains everything required to run with NO network access:
#
#   * the source tree: the files git tracks in the current commit (HEAD),
#     built with `git archive`, exactly as package.sh does. Untracked and
#     ignored files and uncommitted changes are left out (#384).
#   * all five Docker images, pre-built (docker save)
#   * the Ollama model weights (phi3.5 + nomic-embed-text)
#
# Measured at 4.6 GB (2.5 GB images + 2.2 GB model weights) with both models
# present. Build it on a machine where the stack already works,
# then move the file to the target Mac and run install-offline.sh there.

set -euo pipefail
cd "$(dirname "$0")"

# git archive works on the repository, so this directory must be the top level
# of a git checkout. Checked first, before any image or volume is touched.
TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || TOP=""
if [ -z "$TOP" ] || [ "$(cd "$TOP" && pwd -P)" != "$(pwd -P)" ]; then
  echo "ERROR: $(pwd) is not the top level of a git checkout. package-offline.sh archives the tracked files of the current commit with git archive; run it in a clone of the repository." >&2
  exit 1
fi

OUT="${1:-rag-docker-offline.tar}"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"   # absolute

# The MCP server is parked (see docker-compose.yml). Its image is deliberately
# NOT bundled: nothing in the stack uses it, and requiring it here would make
# packaging fail on a machine that has never built it. Re-add
# rag-docker-mcp:latest when the MCP server is brought back.
IMAGES=(
  rag-docker-api:latest
  rag-docker-ui:latest
  semitechnologies/weaviate:1.39.6
  ollama/ollama:0.3.14
  nginx:1.29-alpine
)

echo "==> Checking prerequisites"
for img in "${IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || { echo "ERROR: image '$img' not found locally. Run 'docker compose build' and 'docker compose up -d' first." >&2; exit 1; }
done

# Resolve the compose project so we read the right volume.
PROJECT="$(docker compose config --format json | tr -d ' \n' | sed -n 's/^{"name":"\([^"]*\)".*/\1/p')"
[ -n "$PROJECT" ] || PROJECT="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
MODELVOL="${PROJECT}_ollama_models"
docker volume inspect "$MODELVOL" >/dev/null 2>&1 \
  || { echo "ERROR: volume '$MODELVOL' not found. Start the stack once so the models download." >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/rag-docker/offline"

echo "==> Copying source (tracked files of the current commit)"
# ingest-inbox/.gitkeep and exports/.gitkeep are tracked, so both mount targets
# exist on the target machine; without them Docker creates the directories as
# root and the user cannot drop files into ./ingest-inbox.
git archive --format=tar HEAD | ( cd "$STAGE/rag-docker" && tar xf - )

echo "==> Saving images (this is the slow part)"
docker save "${IMAGES[@]}" | gzip > "$STAGE/rag-docker/offline/images.tar.gz"

echo "==> Exporting model weights from volume '$MODELVOL'"
# Uses the ollama image itself as the tar helper: it ships /usr/bin/tar and is
# already part of the bundle, so no extra image is needed here or on the target.
docker run --rm --entrypoint sh \
  -v "$MODELVOL":/models:ro \
  -v "$STAGE/rag-docker/offline":/out \
  ollama/ollama:0.3.14 \
  -c 'tar czf /out/ollama_models.tar.gz -C /models .'

echo "==> Assembling bundle"
rm -f "$OUT"
tar cf "$OUT" -C "$STAGE" rag-docker

echo
echo "Created $OUT ($(du -h "$OUT" | cut -f1))"
echo "  images:       $(du -h "$STAGE/rag-docker/offline/images.tar.gz" | cut -f1)"
echo "  model weights:$(du -h "$STAGE/rag-docker/offline/ollama_models.tar.gz" | cut -f1)"
echo
echo "On the target Mac (no internet required):"
echo "  tar xf $(basename "$OUT") && cd rag-docker"
echo "  bash install-offline.sh"
