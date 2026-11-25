#!/bin/zsh
# Packs the Claude Desktop extension (integrations/claude-desktop) into dist/smarthome-hub.mcpb.
# Install: Claude Desktop → Settings → Extensions → drag the file in (or double-click it).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
rm -f dist/smarthome-hub.mcpb
(cd integrations/claude-desktop && zip -qr -X ../../dist/smarthome-hub.mcpb manifest.json server -x '*/__pycache__/*')
echo "Built dist/smarthome-hub.mcpb"
