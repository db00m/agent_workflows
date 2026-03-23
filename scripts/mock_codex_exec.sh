#!/usr/bin/env bash
set -euo pipefail

output_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output-last-message)
      output_file="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

if [[ -z "${output_file}" ]]; then
  echo "missing output file" >&2
  exit 1
fi

prompt="$(cat)"

if [[ "${prompt}" == *"workflow step \`implement\`"* ]]; then
  cat > "${output_file}" <<'JSON'
{"status":"fail","handoff_summary":"Implementation blocked in mock mode after plan handoff."}
JSON
else
  cat > "${output_file}" <<'JSON'
{"status":"ok","handoff_summary":"Mock step completed successfully and prepared context for the next step."}
JSON
fi

printf 'mock stdout\n'
