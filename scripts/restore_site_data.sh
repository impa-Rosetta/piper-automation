#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${1:-}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Usage: bash scripts/restore_site_data.sh /path/to/piper_site_data_*.tar.gz" >&2
  exit 2
fi

echo "Archive contents:"
tar -tzf "$ARCHIVE"

while IFS= read -r entry; do
  if [[ "$entry" == /* || "$entry" == ../* || "$entry" == *'/../'* ]]; then
    echo "Unsafe archive path: $entry" >&2
    exit 1
  fi
done < <(tar -tzf "$ARCHIVE")

echo
echo "Existing site files with the same names may be overwritten."
read -r -p "Type RESTORE to continue: " answer
if [[ "$answer" != "RESTORE" ]]; then
  echo "Restore cancelled."
  exit 1
fi

tar -xzf "$ARCHIVE" -C "$PROJECT_ROOT"
echo "Restore complete. Perform a no-load, low-speed validation before production."
