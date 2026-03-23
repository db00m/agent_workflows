#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "get" || "${2:-}" != "role" || "${3:-}" != "--role" || -z "${4:-}" ]]; then
  echo "unsupported mock myteam invocation" >&2
  exit 1
fi

case "$4" in
  analyst)
    printf '%s\n' 'Inspect the repository carefully and summarize its current state.'
    ;;
  planner)
    printf '%s\n' 'Produce an implementation plan with concrete steps and note blockers.'
    ;;
  implementer)
    printf '%s\n' 'Implement the requested changes safely and report what was completed.'
    ;;
  *)
    echo "unknown mock role: $4" >&2
    exit 1
    ;;
esac
