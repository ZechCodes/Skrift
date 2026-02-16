#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

cleanup() {
    echo "Stopping services..."
    podman compose down
}
trap cleanup EXIT

echo "Starting services..."
podman compose up -d --wait

echo "Running integration tests..."
uv run pytest -m integration -v "$@"
