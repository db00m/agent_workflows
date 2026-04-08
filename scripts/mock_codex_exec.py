#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/mock_codex_exec.py",
        description="Minimal mock for `codex exec` workflow testing.",
    )
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--full-auto", action="store_true")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--cd")
    parser.add_argument("--output-schema")
    parser.add_argument("--output-last-message")
    parser.add_argument("--model")
    return parser.parse_args()


def read_prompt(prompt_arg: str | None) -> str:
    if prompt_arg is None or prompt_arg == "-":
        return sys.stdin.read()
    return prompt_arg


def parse_role(prompt: str) -> str:
    first_line = prompt.splitlines()[0] if prompt else ""
    if first_line.startswith("role: ") and ";" in first_line:
        return first_line.split(";", 1)[0].removeprefix("role: ").strip()
    return "unknown"


def parse_context(prompt: str) -> Any:
    marker = "additional_context:\n"
    if marker not in prompt:
        return None
    _, context_text = prompt.split(marker, 1)
    try:
        return json.loads(context_text)
    except json.JSONDecodeError:
        return None


def load_schema(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def value_from_schema(schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if schema_type == "object":
        return {
            key: value_from_schema(value)
            for key, value in schema.get("properties", {}).items()
        }
    if schema_type == "array":
        return []
    if schema_type == "string":
        return "mock"
    if schema_type == "boolean":
        return True
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1
    if schema_type == "null":
        return None
    return {}


def build_reply(role: str, context: Any, schema: dict[str, Any]) -> str:
    if role == "inspector":
        return json.dumps({"valid": True, "result": "inspection ok"})
    if role == "summarizer":
        text = "no inspection result"
        if isinstance(context, dict):
            text = context.get("text", text)
        return json.dumps({"summary": f"Summary: {text}"})
    return json.dumps(value_from_schema(schema))


def main() -> int:
    args = parse_args()
    prompt = read_prompt(args.prompt)
    role = parse_role(prompt)
    context = parse_context(prompt)
    schema = load_schema(args.output_schema)
    reply = build_reply(role, context, schema)

    sys.stdout.write(f"Mock automatic step finished for role {role}.\n")
    sys.stdout.flush()

    if args.output_last_message:
        Path(args.output_last_message).write_text(reply + "\n", encoding="utf-8")
    else:
        sys.stdout.write(reply + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
