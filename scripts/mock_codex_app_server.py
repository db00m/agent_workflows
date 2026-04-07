#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from typing import Any


class MockAppServer:
    def __init__(self) -> None:
        self.thread_counter = 0
        self.turn_counter = 0
        self.item_counter = 0
        self.threads: dict[str, dict[str, Any]] = {}

    def run(self) -> int:
        for raw_line in sys.stdin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            message = json.loads(raw_line)
            if "method" not in message:
                continue

            method = message["method"]
            if method == "initialized":
                continue

            if method == "initialize":
                self._write(
                    {
                        "id": message["id"],
                        "result": {"userAgent": "mock-codex-app-server/1.0"},
                    }
                )
                continue

            if method == "thread/start":
                self._handle_thread_start(message)
                continue

            if method == "turn/start":
                self._handle_turn_start(message)
                continue

            self._write(
                {
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"Unsupported method {method}"},
                }
            )

        return 0

    def _handle_thread_start(self, message: dict[str, Any]) -> None:
        self.thread_counter += 1
        thread_id = f"thread-{self.thread_counter}"
        params = message["params"]
        thread = {
            "id": thread_id,
            "step_id": None,
            "context": None,
            "cwd": params.get("cwd"),
        }
        self.threads[thread_id] = thread

        self._write(
            {
                "id": message["id"],
                "result": {
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "cwd": params.get("cwd"),
                    "model": params.get("model", "mock-model"),
                    "modelProvider": "mock",
                    "sandbox": {"type": "workspaceWrite"},
                    "thread": {
                        "cliVersion": "mock",
                        "createdAt": 0,
                        "cwd": params.get("cwd"),
                        "ephemeral": True,
                        "id": thread_id,
                        "modelProvider": "mock",
                        "preview": "",
                        "source": "appServer",
                        "status": "idle",
                        "turns": [],
                        "updatedAt": 0,
                    },
                },
            }
        )
        self._write(
            {
                "method": "thread/started",
                "params": {
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "cwd": params.get("cwd"),
                    "model": params.get("model", "mock-model"),
                    "modelProvider": "mock",
                    "sandbox": {"type": "workspaceWrite"},
                    "thread": {
                        "cliVersion": "mock",
                        "createdAt": 0,
                        "cwd": params.get("cwd"),
                        "ephemeral": True,
                        "id": thread_id,
                        "modelProvider": "mock",
                        "preview": "",
                        "source": "appServer",
                        "status": "idle",
                        "turns": [],
                        "updatedAt": 0,
                    },
                },
            }
        )

    def _handle_turn_start(self, message: dict[str, Any]) -> None:
        params = message["params"]
        thread_id = params["threadId"]
        thread = self.threads[thread_id]

        self.turn_counter += 1
        turn_id = f"turn-{self.turn_counter}"
        prompt = params["input"][0]["text"]
        self._capture_thread_state(thread, prompt)

        self._write(
            {
                "id": message["id"],
                "result": {"turn": {"id": turn_id, "items": [], "status": "running"}},
            }
        )
        self._write(
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [], "status": "running"},
                },
            }
        )

        reply = self._build_reply(thread, prompt, params.get("outputSchema"))
        self.item_counter += 1
        item_id = f"item-{self.item_counter}"

        self._write(
            {
                "method": "item/started",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {"id": item_id, "text": "", "type": "agentMessage"},
                },
            }
        )
        self._write(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "delta": reply,
                    "itemId": item_id,
                    "threadId": thread_id,
                    "turnId": turn_id,
                },
            }
        )
        self._write(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {"id": item_id, "text": reply, "type": "agentMessage"},
                },
            }
        )
        self._write(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [], "status": "completed"},
                },
            }
        )

    def _capture_thread_state(self, thread: dict[str, Any], prompt: str) -> None:
        if thread["step_id"] is None and prompt.startswith("role: "):
            role_section = prompt.split(";", 1)[0]
            thread["step_id"] = role_section.removeprefix("role: ").strip()

        marker = "additional_context:\n"
        if marker in prompt:
            _, context_text = prompt.split(marker, 1)
            try:
                thread["context"] = json.loads(context_text)
            except json.JSONDecodeError:
                thread["context"] = None

    def _build_reply(
        self,
        thread: dict[str, Any],
        prompt: str,
        output_schema: dict[str, Any] | None,
    ) -> str:
        if prompt.startswith("The user finalized workflow step"):
            return self._build_final_output(thread, output_schema)

        step_id = thread.get("step_id") or "unknown"
        if step_id == "inspector":
            return "Mock inspection complete. Repository structure looks valid."
        if step_id == "summarizer":
            return "Mock summary session ready. I can finalize when you use /done."
        return f"Mock response for role {step_id}."

    def _build_final_output(
        self,
        thread: dict[str, Any],
        output_schema: dict[str, Any] | None,
    ) -> str:
        step_id = thread.get("step_id")
        if step_id == "inspector":
            return json.dumps({"valid": True, "result": "inspection ok"})
        if step_id == "summarizer":
            context = thread.get("context") or {}
            text = context.get("text", "no inspection result")
            return json.dumps({"summary": f"Summary: {text}"})

        return json.dumps(self._value_from_schema(output_schema or {}))

    def _value_from_schema(self, schema: dict[str, Any]) -> Any:
        schema_type = schema.get("type")
        if schema_type == "object":
            return {
                key: self._value_from_schema(value)
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

    def _write(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def main() -> int:
    return MockAppServer().run()


if __name__ == "__main__":
    raise SystemExit(main())
