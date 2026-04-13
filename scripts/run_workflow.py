#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import queue
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:
    print(
        "Missing dependency: PyYAML is required to read workflow files. "
        "Install it with `python3 -m pip install pyyaml`.",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


SCALAR_TYPES = {"string", "boolean", "number", "integer", "null", "any"}
STEP_KEYS = {
    "role",
    "prompt",
    "model",
    "interactive",
    "input",
    "output",
}
APP_SERVER_EOF = object()


class WorkflowError(Exception):
    pass


class RetryStep(Exception):
    pass


class FailWorkflow(Exception):
    pass


@dataclass
class TurnResult:
    turn_id: str
    status: str
    agent_messages: list[str]


class AppServerClient:
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        protocol_path: Path,
        stderr_path: Path,
        model: str | None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.protocol_path = protocol_path
        self.stderr_path = stderr_path
        self.model = model
        self.process: subprocess.Popen[str] | None = None
        self.thread_id: str | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._request_id = 0
        self._protocol_lock = threading.Lock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._protocol_handle = None
        self._stderr_handle = None

    def __enter__(self) -> AppServerClient:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self.protocol_path.parent.mkdir(parents=True, exist_ok=True)
        self._protocol_handle = self.protocol_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")

        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise WorkflowError(
                f"Could not start app server command {self.command[0]}: {exc}"
            ) from exc

        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        request_id = self._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-workflow-runner",
                    "version": "2",
                }
            },
        )
        response = self._wait_for_response(request_id)
        self._raise_on_error(response, "initialize")
        self._send_notification("initialized")

    def close(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None

        if self._protocol_handle is not None:
            self._protocol_handle.close()
            self._protocol_handle = None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None

    def start_thread(self, thread_request_path: Path, thread_response_path: Path) -> str:
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "cwd": str(self.cwd),
            "ephemeral": True,
            "personality": "pragmatic",
            "serviceName": "codex-workflow",
        }
        if self.model:
            params["model"] = self.model

        thread_request_path.write_text(
            json.dumps(params, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        request_id = self._send_request("thread/start", params)
        response = self._wait_for_response(request_id)
        self._raise_on_error(response, "thread/start")

        result = response["result"]
        thread_response_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        thread_id = result["thread"]["id"]
        self.thread_id = thread_id
        return thread_id

    def run_turn(
        self,
        message_text: str,
        output_schema: dict[str, Any] | None,
        request_path: Path,
    ) -> TurnResult:
        if self.thread_id is None:
            raise WorkflowError("Cannot start a turn before creating a thread")

        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": message_text}],
        }
        if output_schema is not None:
            params["outputSchema"] = output_schema

        request_path.write_text(
            json.dumps(params, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        request_id = self._send_request("turn/start", params)
        turn_id: str | None = None
        agent_messages: list[str] = []
        printed_agent_line = False
        streamed_item_ids: set[str] = set()

        while True:
            message = self._next_message()
            if message is APP_SERVER_EOF:
                raise WorkflowError("App server exited unexpectedly")

            if "id" in message and "method" not in message:
                if message["id"] != request_id:
                    continue

                self._raise_on_error(message, "turn/start")
                turn_id = message["result"]["turn"]["id"]
                continue

            if "id" in message and "method" in message:
                raise WorkflowError(
                    f"App server requested unsupported client interaction: {message['method']}"
                )

            method = message.get("method")
            params = message.get("params", {})

            if method == "error":
                details = json.dumps(params, sort_keys=True)
                raise WorkflowError(f"App server reported an error: {details}")

            if method == "item/agentMessage/delta":
                if turn_id is None or params.get("turnId") != turn_id:
                    continue
                if not printed_agent_line:
                    print("Codex: ", end="", flush=True)
                    printed_agent_line = True
                print(params["delta"], end="", flush=True)
                streamed_item_ids.add(params["itemId"])
                continue

            if method == "item/completed":
                if turn_id is None or params.get("turnId") != turn_id:
                    continue
                item = params["item"]
                if item.get("type") == "agentMessage":
                    text = item.get("text", "")
                    if item["id"] not in streamed_item_ids and text:
                        if not printed_agent_line:
                            print("Codex: ", end="", flush=True)
                            printed_agent_line = True
                        print(text, end="", flush=True)
                    agent_messages.append(text)
                continue

            if method == "turn/completed":
                completed_turn = params.get("turn", {})
                if turn_id is None or completed_turn.get("id") != turn_id:
                    continue
                if printed_agent_line:
                    print()

                status = completed_turn["status"]
                if status != "completed":
                    error = completed_turn.get("error")
                    raise WorkflowError(
                        f"Turn {turn_id} ended with status {status}: {json.dumps(error, sort_keys=True)}"
                    )

                return TurnResult(
                    turn_id=turn_id,
                    status=status,
                    agent_messages=agent_messages,
                )

    def _send_request(self, method: str, params: dict[str, Any]) -> int:
        request_id = self._request_id
        self._request_id += 1
        payload = {"id": request_id, "method": method, "params": params}
        self._send_message(payload)
        return request_id

    def _send_notification(self, method: str) -> None:
        self._send_message({"method": method})

    def _send_message(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise WorkflowError("App server process is not running")

        self._write_protocol("out", payload)
        encoded = json.dumps(payload, separators=(",", ":"))
        self.process.stdin.write(encoded + "\n")
        self.process.stdin.flush()

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        while True:
            message = self._next_message()
            if message is APP_SERVER_EOF:
                raise WorkflowError("App server exited unexpectedly")
            if "id" in message and "method" not in message and message["id"] == request_id:
                return message

    def _next_message(self) -> Any:
        message = self._queue.get()
        return message

    def _read_stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for raw_line in self.process.stdout:
            raw_line = raw_line.rstrip("\n")
            if not raw_line:
                continue

            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                payload = {"invalid_json": raw_line}

            self._write_protocol("in", payload)
            self._queue.put(payload)

        self._queue.put(APP_SERVER_EOF)

    def _read_stderr(self) -> None:
        assert self.process is not None
        assert self.process.stderr is not None
        assert self._stderr_handle is not None
        for raw_line in self.process.stderr:
            self._stderr_handle.write(raw_line)
            self._stderr_handle.flush()

    def _write_protocol(self, direction: str, payload: dict[str, Any]) -> None:
        if self._protocol_handle is None:
            return
        with self._protocol_lock:
            self._protocol_handle.write(
                json.dumps({"direction": direction, "message": payload}, sort_keys=True) + "\n"
            )
            self._protocol_handle.flush()

    def _raise_on_error(self, response: dict[str, Any], method: str) -> None:
        if "error" in response:
            raise WorkflowError(
                f"App server {method} failed: {json.dumps(response['error'], sort_keys=True)}"
            )


class WorkflowRunner:
    def __init__(
        self,
        workflow_path: str,
        codex_command: str,
        codex_exec_command: str | None,
        artifacts_dir: str | None,
    ) -> None:
        self.workflow_path = Path(workflow_path).expanduser().resolve()
        self.codex_command = codex_command
        self.codex_exec_command = codex_exec_command or self._derive_exec_command(
            codex_command
        )
        self.invocation_dir = Path.cwd()
        self.artifacts_root = self._resolve_artifacts_root(artifacts_dir)
        self.run_dir = self._build_run_dir()

    def run(self) -> int:
        steps = self._load_workflow()
        self._validate_workflow(steps)

        print(f"Run directory: {self.run_dir}")

        completed_outputs: dict[str, Any] = {}

        for index, step in enumerate(steps, start=1):
            self._print_step_header(
                step_number=index,
                step_id=step["id"],
                interactive=step["_interactive"],
            )
            while True:
                try:
                    result = self._run_step(
                        step=step,
                        completed_outputs=completed_outputs,
                        step_number=index,
                    )
                    completed_outputs[step["id"]] = result
                    break
                except RetryStep:
                    print(f"Retrying step {step['id']}.")
                    continue
                except FailWorkflow:
                    print(f"Workflow failed on step {step['id']}.")
                    return 1

        print("Workflow completed successfully.")
        return 0

    def _load_workflow(self) -> list[dict[str, Any]]:
        try:
            with self.workflow_path.open("r", encoding="utf-8") as handle:
                documents = list(yaml.safe_load_all(handle))
        except FileNotFoundError as exc:
            raise WorkflowError(f"Workflow file not found: {self.workflow_path}") from exc
        except yaml.YAMLError as exc:
            raise WorkflowError(f"Invalid YAML in {self.workflow_path}: {exc}") from exc
        return [document for document in documents if document is not None]

    def _validate_workflow(self, steps: list[dict[str, Any]]) -> None:
        if not isinstance(steps, list) or not steps:
            raise WorkflowError("Workflow must contain at least one YAML document")
        seen_ids: set[str] = set()
        declared_outputs: dict[str, dict[str, Any]] = {}

        for index, document in enumerate(steps, start=1):
            if not isinstance(document, dict):
                raise WorkflowError(f"Workflow document {index} must be a step mapping")
            if len(document) != 1:
                raise WorkflowError(
                    f"Workflow document {index} must contain exactly one top-level step name"
                )

            step_id, step = next(iter(document.items()))
            if not isinstance(step_id, str) or not step_id.strip():
                raise WorkflowError(
                    f"Workflow document {index} must use a non-empty step name as its top-level key"
                )
            if not isinstance(step, dict):
                raise WorkflowError(f"Step {step_id} must map to a step definition")

            extra_step_keys = set(step) - STEP_KEYS
            if extra_step_keys:
                keys = ", ".join(sorted(extra_step_keys))
                raise WorkflowError(f"Unsupported keys in step {step_id}: {keys}")

            role = step.get("role")
            prompt = step.get("prompt")
            output = step.get("output")
            interactive = step.get("interactive", True)

            if step_id in seen_ids:
                raise WorkflowError(f"Duplicate step name: {step_id}")
            if not isinstance(role, str) or not role.strip():
                raise WorkflowError(f"Step {step_id} requires a non-empty `role`")
            if not isinstance(prompt, str) or not prompt.strip():
                raise WorkflowError(f"Step {step_id} requires a non-empty `prompt`")
            if type(interactive) is not bool:
                raise WorkflowError(f"Step {step_id} `interactive` must be a boolean")
            if output is None:
                raise WorkflowError(f"Step {step_id} requires an `output` declaration")

            normalized_output = self._normalize_output_shape(
                output,
                path=f"steps.{step_id}.output",
                require_object=True,
            )

            step["_normalized_output"] = normalized_output
            step["_output_schema"] = self._shape_to_json_schema(normalized_output)
            step["_interactive"] = interactive
            step["_document_index"] = index
            step["id"] = step_id
            steps[index - 1] = step
            declared_outputs[step_id] = normalized_output
            seen_ids.add(step_id)

        for step in steps:
            step_id = step["id"]
            step_input = step.get("input")
            dependencies = self._collect_step_dependencies(
                node=step_input,
                current_step_id=step_id,
                declared_outputs=declared_outputs,
                path=f"steps.{step_id}.input",
            )
            step["_dependencies"] = dependencies

        ordered_steps = self._topologically_order_steps(steps)
        steps[:] = ordered_steps

    def _run_step(
        self,
        step: dict[str, Any],
        completed_outputs: dict[str, Any],
        step_number: int,
    ) -> Any:
        step_id = step["id"]
        step_root = self.run_dir / f"{step_number:02d}-{step_id}"
        step_root.mkdir(parents=True, exist_ok=True)
        attempt = self._next_attempt_number(step_root)
        attempt_dir = step_root / f"attempt-{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        references: list[dict[str, Any]] = []
        resolved_input = self._resolve_reference_nodes(
            node=step.get("input"),
            completed_outputs=completed_outputs,
            path=f"steps.{step_id}.input",
            references=references,
        )

        (attempt_dir / "resolved_input.json").write_text(
            json.dumps(resolved_input, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (attempt_dir / "reference_resolution.json").write_text(
            json.dumps(references, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        initial_prompt = self._build_initial_message(step, resolved_input)
        (attempt_dir / "initial_prompt.txt").write_text(initial_prompt, encoding="utf-8")

        if step["_interactive"]:
            return self._run_interactive_step(
                step=step,
                attempt_dir=attempt_dir,
                initial_prompt=initial_prompt,
            )

        return self._run_automatic_step(
            step=step,
            attempt_dir=attempt_dir,
            initial_prompt=initial_prompt,
        )

    def _run_interactive_step(
        self,
        step: dict[str, Any],
        attempt_dir: Path,
        initial_prompt: str,
    ) -> Any:
        step_id = step["id"]

        protocol_path = attempt_dir / "protocol.jsonl"
        stderr_path = attempt_dir / "stderr.txt"
        transcript_path = attempt_dir / "transcript.txt"
        transcript_lines: list[str] = []

        with AppServerClient(
            command=self._normalized_command_parts(self.codex_command),
            cwd=self.invocation_dir,
            protocol_path=protocol_path,
            stderr_path=stderr_path,
            model=step.get("model"),
        ) as client:
            client.start_thread(
                thread_request_path=attempt_dir / "thread_start_request.json",
                thread_response_path=attempt_dir / "thread_start_response.json",
            )

            self._append_transcript(transcript_lines, "USER", initial_prompt)
            initial_turn = client.run_turn(
                message_text=initial_prompt,
                output_schema=None,
                request_path=attempt_dir / "turn-01-request.json",
            )
            self._record_agent_messages(transcript_lines, initial_turn.agent_messages)

            turn_index = 2
            while True:
                user_input = self._read_user_input(step_id)

                if user_input == "/fail":
                    self._write_transcript(transcript_path, transcript_lines)
                    raise FailWorkflow()

                if user_input == "/retry":
                    self._write_transcript(transcript_path, transcript_lines)
                    raise RetryStep()

                if user_input == "/done":
                    finalize_prompt = self._build_finalize_message(step)
                    self._append_transcript(transcript_lines, "USER", finalize_prompt)
                    final_turn = client.run_turn(
                        message_text=finalize_prompt,
                        output_schema=step["_output_schema"],
                        request_path=attempt_dir / f"turn-{turn_index:02d}-request.json",
                    )
                    self._record_agent_messages(transcript_lines, final_turn.agent_messages)
                    self._write_transcript(transcript_path, transcript_lines)

                    final_message = self._last_agent_message(final_turn.agent_messages, step_id)
                    parsed = self._parse_json_output(final_message, step_id)
                    self._validate_output_value(
                        value=parsed,
                        shape=step["_normalized_output"],
                        path=f"steps.{step_id}.output",
                    )
                    (attempt_dir / "result.json").write_text(
                        json.dumps(parsed, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    print(f"Step {step_id} completed.")
                    return parsed

                self._append_transcript(transcript_lines, "USER", user_input)
                turn = client.run_turn(
                    message_text=user_input,
                    output_schema=None,
                    request_path=attempt_dir / f"turn-{turn_index:02d}-request.json",
                )
                self._record_agent_messages(transcript_lines, turn.agent_messages)
                turn_index += 1

    def _run_automatic_step(
        self,
        step: dict[str, Any],
        attempt_dir: Path,
        initial_prompt: str,
    ) -> Any:
        step_id = step["id"]
        stdout_path = attempt_dir / "stdout.txt"
        stderr_path = attempt_dir / "stderr.txt"
        transcript_path = attempt_dir / "transcript.txt"
        output_schema_path = attempt_dir / "output_schema.json"
        last_message_path = attempt_dir / "last_message.txt"
        request_path = attempt_dir / "exec_request.json"
        transcript_lines: list[str] = []

        output_schema_path.write_text(
            json.dumps(step["_output_schema"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        command = self._normalized_command_parts(self.codex_exec_command)
        command.extend(
            [
                "--full-auto",
                "--ephemeral",
                "--cd",
                str(self.invocation_dir),
                "--output-schema",
                str(output_schema_path),
                "--output-last-message",
                str(last_message_path),
            ]
        )
        if step.get("model"):
            command.extend(["--model", step["model"]])
        command.append(initial_prompt)

        request_path.write_text(
            json.dumps(
                {
                    "command": command,
                    "cwd": str(self.invocation_dir),
                    "output_schema": step["_output_schema"],
                    "step_id": step_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        self._append_transcript(transcript_lines, "USER", initial_prompt)

        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_handle:
                process = subprocess.Popen(
                    command,
                    cwd=self.invocation_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                stdout_thread = threading.Thread(
                    target=self._stream_exec_output,
                    args=(process.stdout, stdout_handle, sys.stdout),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=self._stream_exec_output,
                    args=(process.stderr, stderr_handle, sys.stderr),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                return_code = process.wait()
                stdout_thread.join()
                stderr_thread.join()
        except FileNotFoundError as exc:
            raise WorkflowError(
                f"Could not start automatic step command {command[0]}: {exc}"
            ) from exc

        if return_code != 0:
            raise WorkflowError(
                f"Automatic step {step_id} failed with exit code {return_code}"
            )

        try:
            final_message = last_message_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise WorkflowError(
                f"Automatic step {step_id} did not produce a last message artifact"
            ) from exc

        if not final_message:
            raise WorkflowError(f"Automatic step {step_id} returned an empty final message")

        self._append_transcript(transcript_lines, "CODEX", final_message)
        self._write_transcript(transcript_path, transcript_lines)

        parsed = self._parse_json_output(final_message, step_id)
        self._validate_output_value(
            value=parsed,
            shape=step["_normalized_output"],
            path=f"steps.{step_id}.output",
        )
        (attempt_dir / "result.json").write_text(
            json.dumps(parsed, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Step {step_id} completed.")
        return parsed

    def _normalize_output_shape(
        self,
        node: Any,
        path: str,
        require_object: bool = False,
    ) -> dict[str, Any]:
        if isinstance(node, dict):
            properties: dict[str, Any] = {}
            for key, value in node.items():
                if not isinstance(key, str) or not key:
                    raise WorkflowError(f"{path} contains a non-string or empty object key")
                properties[key] = self._normalize_output_shape(
                    value,
                    path=f"{path}.{key}",
                )

            normalized = {"type": "object", "properties": properties}
            if require_object and not properties:
                raise WorkflowError(f"{path} must declare at least one output field")
            return normalized

        if isinstance(node, list):
            if len(node) != 1:
                raise WorkflowError(f"{path} array declarations must contain exactly one item")
            return {
                "type": "array",
                "items": self._normalize_output_shape(node[0], path=f"{path}[]"),
            }

        if isinstance(node, str):
            if node not in SCALAR_TYPES:
                raise WorkflowError(
                    f"{path} uses unsupported scalar type `{node}`"
                )
            return {"type": node}

        raise WorkflowError(
            f"{path} must be a scalar type name, object mapping, or single-item array declaration"
        )

    def _shape_to_json_schema(self, shape: dict[str, Any]) -> dict[str, Any]:
        node_type = shape["type"]
        if node_type == "object":
            properties = {
                key: self._shape_to_json_schema(value)
                for key, value in shape["properties"].items()
            }
            return {
                "type": "object",
                "properties": properties,
                "required": sorted(properties),
                "additionalProperties": False,
            }
        if node_type == "array":
            return {
                "type": "array",
                "items": self._shape_to_json_schema(shape["items"]),
            }
        if node_type == "any":
            return {}
        return {"type": node_type}

    def _validate_reference_nodes(
        self,
        node: Any,
        current_step_id: str,
        declared_outputs: dict[str, dict[str, Any]],
        path: str,
        dependencies: set[str],
    ) -> None:
        if isinstance(node, dict):
            if "$ref" in node:
                if set(node) != {"$ref"}:
                    raise WorkflowError(
                        f"{path} reference objects may only contain `$ref`"
                    )
                ref_path = node["$ref"]
                if not isinstance(ref_path, str) or not ref_path:
                    raise WorkflowError(f"{path} `$ref` must be a non-empty string")
                target_step_id = self._validate_ref_path(
                    ref_path, current_step_id, declared_outputs, path
                )
                dependencies.add(target_step_id)
                return

            for key, value in node.items():
                if not isinstance(key, str) or not key:
                    raise WorkflowError(f"{path} contains a non-string or empty key")
                self._validate_reference_nodes(
                    value,
                    current_step_id=current_step_id,
                    declared_outputs=declared_outputs,
                    path=f"{path}.{key}",
                    dependencies=dependencies,
                )
            return

        if isinstance(node, list):
            for index, value in enumerate(node):
                self._validate_reference_nodes(
                    value,
                    current_step_id=current_step_id,
                    declared_outputs=declared_outputs,
                    path=f"{path}[{index}]",
                    dependencies=dependencies,
                )

    def _collect_step_dependencies(
        self,
        node: Any,
        current_step_id: str,
        declared_outputs: dict[str, dict[str, Any]],
        path: str,
    ) -> set[str]:
        dependencies: set[str] = set()
        if node is not None:
            self._validate_reference_nodes(
                node=node,
                current_step_id=current_step_id,
                declared_outputs=declared_outputs,
                path=path,
                dependencies=dependencies,
            )
        return dependencies

    def _validate_ref_path(
        self,
        ref_path: str,
        current_step_id: str,
        declared_outputs: dict[str, dict[str, Any]],
        path: str,
    ) -> str:
        segments = ref_path.split(".")
        if len(segments) < 2 or segments[1] != "output":
            raise WorkflowError(
                f"{path} has invalid `$ref` `{ref_path}`; expected `<step_name>.output...`"
            )

        target_step_id = segments[0]
        if target_step_id == current_step_id:
            raise WorkflowError(f"{path} cannot reference the current step `{current_step_id}`")
        if target_step_id not in declared_outputs:
            raise WorkflowError(f"{path} references unknown step `{target_step_id}`")

        shape = declared_outputs[target_step_id]
        for segment in segments[2:]:
            if shape["type"] != "object":
                raise WorkflowError(
                    f"{path} references `{ref_path}`, but `{segment}` does not exist on a non-object value"
                )
            properties = shape["properties"]
            if segment not in properties:
                raise WorkflowError(
                    f"{path} references missing output field `{segment}` in `{ref_path}`"
                )
            shape = properties[segment]
        return target_step_id

    def _topologically_order_steps(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        step_by_id = {step["id"]: step for step in steps}
        incoming_counts = {
            step["id"]: len(step["_dependencies"])
            for step in steps
        }
        dependents: dict[str, list[str]] = {step["id"]: [] for step in steps}

        for step in steps:
            for dependency in step["_dependencies"]:
                dependents[dependency].append(step["id"])

        ready = sorted(
            [step["id"] for step in steps if incoming_counts[step["id"]] == 0],
            key=lambda step_id: step_by_id[step_id]["_document_index"],
        )
        ordered_steps: list[dict[str, Any]] = []

        while ready:
            step_id = ready.pop(0)
            ordered_steps.append(step_by_id[step_id])

            newly_ready: list[str] = []
            for dependent_id in dependents[step_id]:
                incoming_counts[dependent_id] -= 1
                if incoming_counts[dependent_id] == 0:
                    newly_ready.append(dependent_id)
            ready.extend(newly_ready)
            ready.sort(key=lambda ready_id: step_by_id[ready_id]["_document_index"])

        if len(ordered_steps) != len(steps):
            blocked = sorted(
                [
                    step_id
                    for step_id, count in incoming_counts.items()
                    if count > 0
                ]
            )
            raise WorkflowError(
                "Workflow dependencies contain a cycle involving: "
                + ", ".join(blocked)
            )

        return ordered_steps

    def _resolve_reference_nodes(
        self,
        node: Any,
        completed_outputs: dict[str, Any],
        path: str,
        references: list[dict[str, Any]],
    ) -> Any:
        if node is None:
            return None

        if isinstance(node, dict):
            if "$ref" in node:
                ref_path = node["$ref"]
                value = self._resolve_ref_value(ref_path, completed_outputs)
                references.append(
                    {
                        "context_path": path,
                        "ref": ref_path,
                        "value": copy.deepcopy(value),
                    }
                )
                return copy.deepcopy(value)

            return {
                key: self._resolve_reference_nodes(
                    value,
                    completed_outputs=completed_outputs,
                    path=f"{path}.{key}",
                    references=references,
                )
                for key, value in node.items()
            }

        if isinstance(node, list):
            return [
                self._resolve_reference_nodes(
                    value,
                    completed_outputs=completed_outputs,
                    path=f"{path}[{index}]",
                    references=references,
                )
                for index, value in enumerate(node)
            ]

        return copy.deepcopy(node)

    def _resolve_ref_value(
        self,
        ref_path: str,
        completed_outputs: dict[str, Any],
    ) -> Any:
        segments = ref_path.split(".")
        step_id = segments[0]
        if step_id not in completed_outputs:
            raise WorkflowError(f"Reference `{ref_path}` has no completed output")

        value: Any = completed_outputs[step_id]
        for segment in segments[2:]:
            if not isinstance(value, dict) or segment not in value:
                raise WorkflowError(f"Reference `{ref_path}` did not resolve at runtime")
            value = value[segment]
        return value

    def _validate_output_value(
        self,
        value: Any,
        shape: dict[str, Any],
        path: str,
    ) -> None:
        node_type = shape["type"]

        if node_type == "any":
            return

        if node_type == "object":
            if not isinstance(value, dict):
                raise WorkflowError(f"{path} must be an object")

            expected_keys = set(shape["properties"])
            actual_keys = set(value)
            missing = expected_keys - actual_keys
            extra = actual_keys - expected_keys

            if missing:
                keys = ", ".join(sorted(missing))
                raise WorkflowError(f"{path} is missing required keys: {keys}")
            if extra:
                keys = ", ".join(sorted(extra))
                raise WorkflowError(f"{path} has unexpected keys: {keys}")

            for key, child_shape in shape["properties"].items():
                self._validate_output_value(
                    value=value[key],
                    shape=child_shape,
                    path=f"{path}.{key}",
                )
            return

        if node_type == "array":
            if not isinstance(value, list):
                raise WorkflowError(f"{path} must be an array")
            for index, item in enumerate(value):
                self._validate_output_value(
                    value=item,
                    shape=shape["items"],
                    path=f"{path}[{index}]",
                )
            return

        validators = {
            "string": lambda item: isinstance(item, str),
            "boolean": lambda item: type(item) is bool,
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "null": lambda item: item is None,
        }

        if not validators[node_type](value):
            raise WorkflowError(f"{path} must be of type `{node_type}`")

    def _build_initial_message(self, step: dict[str, Any], resolved_input: Any) -> str:
        lines = [f"role: {step['role']}; task: {step['prompt'].rstrip()}"]
        if not step["_interactive"]:
            lines.extend(
                [
                    "",
                    "You are working inside an automatic workflow step.",
                    "Complete the task in one pass and return only the final JSON object.",
                    "Do not include markdown fences or explanatory text.",
                    "",
                    "input:",
                    json.dumps(resolved_input, indent=2, sort_keys=True),
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "You are working inside an interactive workflow step.",
                    "Collaborate with the user until they decide to finalize the step.",
                    "",
                    "input:",
                    json.dumps(resolved_input, indent=2, sort_keys=True),
                ]
            )
        return "\n".join(lines)

    def _build_finalize_message(self, step: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"The user finalized workflow step `{step['id']}`.",
                "Return only the final JSON object for this step.",
                "Do not include markdown fences or explanatory text.",
            ]
        )

    def _read_user_input(self, step_id: str) -> str:
        try:
            return input(f"[{step_id}] Enter a message or /done /retry /fail: ").strip()
        except EOFError as exc:
            raise WorkflowError("Workflow input closed while a step was active") from exc

    def _record_agent_messages(
        self,
        transcript_lines: list[str],
        agent_messages: list[str],
    ) -> None:
        for message in agent_messages:
            self._append_transcript(transcript_lines, "CODEX", message)

    def _append_transcript(self, transcript_lines: list[str], speaker: str, text: str) -> None:
        transcript_lines.append(f"{speaker}:")
        transcript_lines.append(text.rstrip())
        transcript_lines.append("")

    def _write_transcript(self, transcript_path: Path, transcript_lines: list[str]) -> None:
        transcript_path.write_text("\n".join(transcript_lines).rstrip() + "\n", encoding="utf-8")

    def _last_agent_message(self, agent_messages: list[str], step_id: str) -> str:
        for message in reversed(agent_messages):
            if message.strip():
                return message
        raise WorkflowError(f"Step {step_id} did not produce a final agent message")

    def _parse_json_output(self, final_message: str, step_id: str) -> Any:
        try:
            return json.loads(final_message)
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"Step {step_id} returned invalid JSON during finalization: {exc}"
            ) from exc

    def _next_attempt_number(self, step_root: Path) -> int:
        attempts = [
            child
            for child in step_root.iterdir()
            if child.is_dir() and child.name.startswith("attempt-")
        ]
        return len(attempts) + 1

    def _stream_exec_output(
        self,
        stream: Any,
        artifact_handle: Any,
        display_handle: Any,
    ) -> None:
        if stream is None:
            return
        for raw_line in stream:
            artifact_handle.write(raw_line)
            artifact_handle.flush()
            display_handle.write(raw_line)
            display_handle.flush()

    def _normalized_command_parts(self, command: str) -> list[str]:
        parts = shlex.split(command)
        if not parts:
            raise WorkflowError("Codex command cannot be empty")

        executable = Path(parts[0]).expanduser()
        if "/" in parts[0]:
            if not executable.is_absolute():
                executable = (self.invocation_dir / executable).resolve()
            parts[0] = str(executable)

        return parts

    def _derive_exec_command(self, app_server_command: str) -> str:
        parts = shlex.split(app_server_command)
        if parts and parts[-1] == "app-server":
            parts[-1] = "exec"
            return shlex.join(parts)
        return "codex exec"

    def _resolve_artifacts_root(self, artifacts_dir: str | None) -> Path:
        if artifacts_dir:
            return Path(artifacts_dir).expanduser().resolve()
        return self.invocation_dir / ".codex-workflow"

    def _build_run_dir(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        run_dir = self.artifacts_root / "runs" / timestamp
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _print_step_header(self, step_number: int, step_id: str, interactive: bool) -> None:
        mode = "interactive" if interactive else "automatic"
        title = f"Step {step_number}: {step_id} ({mode})"
        border = "=" * len(title)
        print()
        print(border)
        print(title)
        print(border)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/run_workflow.py",
        description="Run a YAML-defined Codex workflow with interactive and automatic steps.",
    )
    parser.add_argument("workflow_file", help="Path to the workflow YAML file")
    parser.add_argument(
        "--codex-command",
        default="codex app-server",
        help="Command used to invoke Codex app-server for interactive steps",
    )
    parser.add_argument(
        "--codex-exec-command",
        help=(
            "Command used to invoke Codex exec for automatic steps. "
            "Defaults to a value derived from --codex-command when possible, "
            "otherwise `codex exec`."
        ),
    )
    parser.add_argument(
        "--artifacts-dir",
        help=(
            "Directory where workflow run artifacts are stored. "
            "Defaults to ./.codex-workflow in the current shell directory."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = WorkflowRunner(
        workflow_path=args.workflow_file,
        codex_command=args.codex_command,
        codex_exec_command=args.codex_exec_command,
        artifacts_dir=args.artifacts_dir,
    )
    return runner.run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WorkflowError as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        sys.exit(1)
