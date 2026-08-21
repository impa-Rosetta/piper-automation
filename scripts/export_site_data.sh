#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$PROJECT_ROOT/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE="$OUTPUT_DIR/piper_site_data_$STAMP.tar.gz"

mkdir -p "$OUTPUT_DIR"
cd "$PROJECT_ROOT"

CANDIDATES=(
  teach/production_tasks
  teach/feeder_above.json
  teach/home.json
  teach/zero_home.json
  config/windows_remote_workbench.json
  records
)

EXISTING=()
for path in "${CANDIDATES[@]}"; do
  [[ -e "$path" ]] && EXISTING+=("$path")
done

if [[ ${#EXISTING[@]} -eq 0 ]]; then
  echo "No site-specific data was found under $PROJECT_ROOT" >&2
  exit 1
fi

tar -czf "$ARCHIVE" "${EXISTING[@]}"
sha256sum "$ARCHIVE" >"$ARCHIVE.sha256"
echo "Site data exported:"
echo "  $ARCHIVE"
echo "  $ARCHIVE.sha256"
echo "Keep this archive off GitHub and store it on two independent media."
