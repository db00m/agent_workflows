#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "schemas" / "step-output.schema.json"
)


class WorkflowError(Exception):
    """Raised when the workflow file or step execution is invalid."""


@dataclass(frozen=True)
class WorkflowDefaults:
    model: str | None = None
    sandbox: str | None = None
    profile: str | None = None


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    prompt: str
    model: str | None = None
    sandbox: str | None = None
    profile: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    version: int
    defaults: WorkflowDefaults
    steps: list[WorkflowStep]


@dataclass(frozen=True)
class OutputFieldSchema:
    field_type: str
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True)
class OutputSchema:
    required: tuple[str, ...]
    properties: dict[str, OutputFieldSchema]
    additional_properties: bool = True


class WorkflowRunner:
    def __init__(self, workflow_path: str, schema_path: str, codex_command: str) -> None:
        self.workflow_path = Path(workflow_path).expanduser().resolve()
        self.schema_path = Path(schema_path).expanduser().resolve()
        self.codex_command = codex_command
        self.invocation_dir = Path.cwd()
        self.workflow_dir = self.workflow_path.parent
        self.run_dir = self._build_run_dir()
        self.output_schema: OutputSchema | None = None

    def run(self) -> int:
        workflow = self.load_workflow()
        handoff_summary: str | None = None

        print(f"Run directory: {self.run_dir}")

        for step_number, step in enumerate(workflow.steps, start=1):
            result = self._run_step(
                step=step,
                defaults=workflow.defaults,
                previous_handoff=handoff_summary,
                step_number=step_number,
            )
            handoff_summary = result["handoff_summary"]

            if result["status"] == "fail":
                print(f"Workflow halted on step {step.id}: {handoff_summary}")
                return 1

        print("Workflow completed successfully.")
        return 0

    def load_workflow(self) -> WorkflowDefinition:
        data = self._load_yaml()
        return self._parse_workflow(data)

    def _load_yaml(self) -> dict[str, Any]:
        try:
            with self.workflow_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
        except FileNotFoundError as exc:
            raise WorkflowError(f"Workflow file not found: {self.workflow_path}") from exc
        except yaml.YAMLError as exc:
            raise WorkflowError(f"Invalid YAML in {self.workflow_path}: {exc}") from exc

        if not isinstance(loaded, dict):
            raise WorkflowError("Workflow must be a mapping")

        return loaded

    def _parse_workflow(self, workflow: dict[str, Any]) -> WorkflowDefinition:
        if workflow.get("version") != 1:
            raise WorkflowError("Workflow version must be 1")

        defaults = self._parse_defaults(workflow.get("defaults"))
        steps_value = workflow.get("steps")

        if not isinstance(steps_value, list) or not steps_value:
            raise WorkflowError("`steps` must be a non-empty array")

        steps: list[WorkflowStep] = []
        step_ids: set[str] = set()

        for index, raw_step in enumerate(steps_value, start=1):
            if not isinstance(raw_step, dict):
                raise WorkflowError(f"Step {index} must be a mapping")

            step = self._parse_step(raw_step, index)
            if step.id in step_ids:
                raise WorkflowError(f"Duplicate step id: {step.id}")

            step_ids.add(step.id)
            steps.append(step)

        self.output_schema = self._load_output_schema()

        return WorkflowDefinition(version=1, defaults=defaults, steps=steps)

    def _load_output_schema(self) -> OutputSchema:
        try:
            loaded = json.loads(self.schema_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkflowError(f"Schema file not found: {self.schema_path}") from exc
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"Invalid JSON schema in {self.schema_path}: {exc}") from exc

        if not isinstance(loaded, dict):
            raise WorkflowError("Step output schema must be a JSON object")
        if loaded.get("type") != "object":
            raise WorkflowError("Step output schema must declare type `object`")

        properties = loaded.get("properties")
        required = loaded.get("required", [])
        additional_properties = loaded.get("additionalProperties", True)

        if not isinstance(properties, dict) or not properties:
            raise WorkflowError("Step output schema must define object properties")
        if not isinstance(required, list) or not all(
            isinstance(item, str) and item for item in required
        ):
            raise WorkflowError("Step output schema `required` must be a list of strings")
        if not isinstance(additional_properties, bool):
            raise WorkflowError(
                "Step output schema `additionalProperties` must be a boolean"
            )

        parsed_properties: dict[str, OutputFieldSchema] = {}
        for name, definition in properties.items():
            if not isinstance(definition, dict):
                raise WorkflowError(f"Schema property `{name}` must be a JSON object")

            field_type = definition.get("type")
            if field_type not in {"string"}:
                raise WorkflowError(
                    f"Schema property `{name}` must declare supported type `string`"
                )

            enum = definition.get("enum")
            parsed_enum: tuple[str, ...] | None = None
            if enum is not None:
                if not isinstance(enum, list) or not enum or not all(
                    isinstance(item, str) for item in enum
                ):
                    raise WorkflowError(
                        f"Schema property `{name}` enum must be a non-empty list of strings"
                    )
                parsed_enum = tuple(enum)

            parsed_properties[name] = OutputFieldSchema(
                field_type=field_type,
                enum=parsed_enum,
            )

        return OutputSchema(
            required=tuple(required),
            properties=parsed_properties,
            additional_properties=additional_properties,
        )

    def _parse_defaults(self, raw_defaults: Any) -> WorkflowDefaults:
        if raw_defaults is None:
            return WorkflowDefaults()
        if not isinstance(raw_defaults, dict):
            raise WorkflowError("`defaults` must be a mapping")

        self._validate_string_field(raw_defaults, "model", "`defaults.model`")
        self._validate_string_field(raw_defaults, "sandbox", "`defaults.sandbox`")
        self._validate_string_field(raw_defaults, "profile", "`defaults.profile`")

        return WorkflowDefaults(
            model=raw_defaults.get("model"),
            sandbox=raw_defaults.get("sandbox"),
            profile=raw_defaults.get("profile"),
        )

    def _parse_step(self, raw_step: dict[str, Any], index: int) -> WorkflowStep:
        step_id = raw_step.get("id")
        prompt = raw_step.get("prompt")

        if not isinstance(step_id, str) or not step_id.strip():
            raise WorkflowError(f"Step {index} requires a non-empty `id`")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError(f"Step {step_id} requires a non-empty `prompt`")

        self._validate_string_field(raw_step, "model", f"Step {step_id} `model`")
        self._validate_string_field(raw_step, "sandbox", f"Step {step_id} `sandbox`")
        self._validate_string_field(raw_step, "profile", f"Step {step_id} `profile`")

        return WorkflowStep(
            id=step_id.strip(),
            prompt=prompt.strip(),
            model=raw_step.get("model"),
            sandbox=raw_step.get("sandbox"),
            profile=raw_step.get("profile"),
        )

    def _validate_string_field(
        self, values: dict[str, Any], field_name: str, label: str
    ) -> None:
        value = values.get(field_name)
        if value is not None and not isinstance(value, str):
            raise WorkflowError(f"{label} must be a string")

    def _run_step(
        self,
        step: WorkflowStep,
        defaults: WorkflowDefaults,
        previous_handoff: str | None,
        step_number: int,
    ) -> dict[str, Any]:
        step_dir = self.run_dir / f"{step_number:02d}-{step.id}"
        step_dir.mkdir(parents=True, exist_ok=True)

        prompt = self._build_prompt(step.id, step.prompt, previous_handoff)
        prompt_path = step_dir / "prompt.txt"
        stdout_path = step_dir / "stdout.txt"
        stderr_path = step_dir / "stderr.txt"
        output_path = step_dir / "result.json"
        command_path = step_dir / "command.txt"

        prompt_path.write_text(prompt, encoding="utf-8")

        command = self._build_command(step, defaults, output_path)
        command_path.write_text(shlex.join(command), encoding="utf-8")

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=self.workflow_dir,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorkflowError(
                f"Step {step.id} could not start command {command[0]}: {exc}"
            ) from exc

        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")

        if completed.returncode != 0:
            raise WorkflowError(
                f"Step {step.id} failed to execute codex command. See {stderr_path}"
            )

        parsed = self._parse_step_output(output_path, step.id)
        self._validate_step_output(parsed, step.id)

        print(f"Step {step.id}: {parsed['status']}")
        return parsed

    def _build_prompt(
        self, step_id: str, step_prompt: str, previous_handoff: str | None
    ) -> str:
        lines = [f"You are executing workflow step `{step_id}`.", ""]

        if previous_handoff:
            lines.extend(["Previous handoff summary:", previous_handoff, ""])

        lines.extend(
            [
                "Return a JSON object that matches the provided output schema.",
                "Set `status` to `ok` only if this step completed successfully.",
                "Set `status` to `fail` if the workflow should stop.",
                "Put a concise operational summary in `handoff_summary`.",
                "",
                "Step instructions:",
                step_prompt,
            ]
        )

        return "\n".join(lines)

    def _build_command(
        self, step: WorkflowStep, defaults: WorkflowDefaults, output_path: Path
    ) -> list[str]:
        command = self._normalized_command_parts()
        if command[-1] != "exec":
            command.append("exec")

        command.extend(
            [
                "--skip-git-repo-check",
                "--output-schema",
                str(self.schema_path),
                "-o",
                str(output_path),
            ]
        )

        model = step.model or defaults.model
        sandbox = step.sandbox or defaults.sandbox
        profile = step.profile or defaults.profile

        if model:
            command.extend(["--model", model])
        if sandbox:
            command.extend(["--sandbox", sandbox])
        if profile:
            command.extend(["--profile", profile])

        command.append("-")
        return command

    def _normalized_command_parts(self) -> list[str]:
        parts = shlex.split(self.codex_command)
        if not parts:
            raise WorkflowError("Codex command cannot be empty")

        executable = Path(parts[0]).expanduser()
        if "/" in parts[0]:
            if not executable.is_absolute():
                executable = (self.invocation_dir / executable).resolve()
            parts[0] = str(executable)

        return parts

    def _parse_step_output(self, output_path: Path, step_id: str) -> dict[str, Any]:
        if not output_path.is_file():
            raise WorkflowError(f"Step {step_id} did not write {output_path}")

        try:
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"Step {step_id} wrote invalid JSON to {output_path}: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise WorkflowError(f"Step {step_id} output must be a JSON object")

        return parsed

    def _validate_step_output(self, parsed: dict[str, Any], step_id: str) -> None:
        if self.output_schema is None:
            self.output_schema = self._load_output_schema()

        schema = self.output_schema
        missing_keys = set(schema.required) - parsed.keys()
        if missing_keys:
            missing = ", ".join(sorted(missing_keys))
            raise WorkflowError(f"Step {step_id} output missing keys: {missing}")

        if not schema.additional_properties:
            extra_keys = parsed.keys() - schema.properties.keys()
            if extra_keys:
                extra = ", ".join(sorted(extra_keys))
                raise WorkflowError(f"Step {step_id} output has unexpected keys: {extra}")

        for field_name, definition in schema.properties.items():
            if field_name not in parsed:
                continue

            value = parsed[field_name]
            if definition.field_type == "string" and not isinstance(value, str):
                raise WorkflowError(
                    f"Step {step_id} output `{field_name}` must be a string"
                )

            if definition.enum is not None and value not in definition.enum:
                allowed = ", ".join(f"`{item}`" for item in definition.enum)
                raise WorkflowError(
                    f"Step {step_id} output `{field_name}` must be one of {allowed}"
                )

    def _build_run_dir(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        runs_dir = self.workflow_dir / ".codex-workflow" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        candidate = runs_dir / timestamp
        suffix = 1
        while candidate.exists():
            candidate = runs_dir / f"{timestamp}-{suffix:02d}"
            suffix += 1

        candidate.mkdir()
        return candidate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/run_workflow.py",
        description="Run a YAML-defined Codex workflow.",
    )
    parser.add_argument("workflow_file", help="Path to the workflow YAML file")
    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA_PATH),
        help="Path to the step output schema file",
    )
    parser.add_argument(
        "--codex-command",
        default="codex",
        help="Command used to invoke Codex",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = WorkflowRunner(
        workflow_path=args.workflow_file,
        schema_path=args.schema,
        codex_command=args.codex_command,
    )
    return runner.run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WorkflowError as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        sys.exit(1)
