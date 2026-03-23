#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:
    print(
        "Missing dependency: PyYAML is required to read workflow files. "
        "Install it with `python3 -m pip install pyyaml`.",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


STEP_RESULT_KEYS = {"status", "handoff_summary"}


class WorkflowError(Exception):
    pass


class WorkflowRunner:
    def __init__(
        self,
        workflow_path: str,
        schema_path: str,
        codex_command: str,
        artifacts_dir: str | None,
    ) -> None:
        self.workflow_path = Path(workflow_path).expanduser().resolve()
        self.schema_path = Path(schema_path).expanduser().resolve()
        self.codex_command = codex_command
        self.invocation_dir = Path.cwd()
        self.workflow_dir = self.workflow_path.parent
        self.artifacts_root = self._resolve_artifacts_root(artifacts_dir)
        self.run_dir = self._build_run_dir()

    def run(self) -> int:
        workflow = self._load_workflow()
        self._validate_workflow(workflow)
        handoff_summary: str | None = None

        print(f"Run directory: {self.run_dir}")

        for index, step in enumerate(workflow["steps"], start=1):
            step_result = self._run_step(
                step=step,
                defaults=workflow.get("defaults", {}),
                previous_handoff=handoff_summary,
                step_number=index,
            )
            handoff_summary = step_result["handoff_summary"]

            if step_result["status"] == "fail":
                print(f"Workflow halted on step {step['id']}: {handoff_summary}")
                return 1

        print("Workflow completed successfully.")
        return 0

    def _load_workflow(self) -> dict:
        try:
            with self.workflow_path.open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        except FileNotFoundError as exc:
            raise WorkflowError(f"Workflow file not found: {self.workflow_path}") from exc
        except yaml.YAMLError as exc:
            raise WorkflowError(f"Invalid YAML in {self.workflow_path}: {exc}") from exc

    def _validate_workflow(self, workflow: dict) -> None:
        if not isinstance(workflow, dict):
            raise WorkflowError("Workflow must be a mapping")
        if workflow.get("version") != 1:
            raise WorkflowError("Workflow version must be 1")

        defaults = workflow.get("defaults")
        if defaults is not None and not isinstance(defaults, dict):
            raise WorkflowError("`defaults` must be a mapping")

        steps = workflow.get("steps")
        if not isinstance(steps, list) or not steps:
            raise WorkflowError("`steps` must be a non-empty array")

        ids: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                raise WorkflowError("Each step must be a mapping")

            step_id = step.get("id")
            prompt = step.get("prompt")

            if not isinstance(step_id, str) or not step_id:
                raise WorkflowError("Each step requires a non-empty `id`")
            if step_id in ids:
                raise WorkflowError(f"Duplicate step id: {step_id}")
            if not isinstance(prompt, str) or not prompt.strip():
                raise WorkflowError(f"Step {step_id} requires a non-empty `prompt`")

            ids.add(step_id)

        if not self.schema_path.is_file():
            raise WorkflowError(f"Schema file not found: {self.schema_path}")

    def _run_step(
        self,
        step: dict,
        defaults: dict,
        previous_handoff: str | None,
        step_number: int,
    ) -> dict:
        step_id = step["id"]
        step_dir = self.run_dir / f"{step_number:02d}-{step_id}"
        step_dir.mkdir(parents=True, exist_ok=True)

        prompt = self._build_prompt(step_id, step["prompt"], previous_handoff)
        prompt_path = step_dir / "prompt.txt"
        output_path = step_dir / "result.json"
        stdout_path = step_dir / "stdout.txt"
        stderr_path = step_dir / "stderr.txt"
        command_path = step_dir / "command.txt"

        prompt_path.write_text(prompt, encoding="utf-8")

        command = self._build_command(step, defaults, output_path)
        command_path.write_text(shlex.join(command), encoding="utf-8")

        try:
            result = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=self.workflow_dir,
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorkflowError(
                f"Step {step_id} could not start command {command[0]}: {exc}"
            ) from exc

        step_dir.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        if result.returncode != 0:
            raise WorkflowError(
                f"Step {step_id} failed to execute codex command. See {stderr_path}"
            )

        parsed = self._parse_step_output(output_path, step_id)
        self._validate_step_output(parsed, step_id)

        print(f"Step {step_id}: {parsed['status']}")
        return parsed

    def _build_prompt(
        self, step_id: str, step_prompt: str, previous_handoff: str | None
    ) -> str:
        lines = [f"You are executing workflow step `{step_id}`.", ""]
        if previous_handoff:
            lines.extend(
                [
                    "Previous handoff summary:",
                    previous_handoff,
                    "",
                ]
            )

        lines.extend(
            [
                "Return a JSON object that matches the provided output schema.",
                "Set `status` to `ok` only if this step completed successfully.",
                "Set `status` to `fail` if the workflow should stop.",
                "Put a concise operational summary in `handoff_summary`.",
                "",
                "Step instructions:",
                step_prompt.rstrip(),
            ]
        )
        return "\n".join(lines)

    def _build_command(self, step: dict, defaults: dict, output_path: Path) -> list[str]:
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

        model = step.get("model") or defaults.get("model")
        sandbox = step.get("sandbox") or defaults.get("sandbox")
        profile = step.get("profile") or defaults.get("profile")

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

    def _parse_step_output(self, output_path: Path, step_id: str) -> dict:
        if not output_path.is_file():
            raise WorkflowError(f"Step {step_id} did not write {output_path}")

        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"Step {step_id} wrote invalid JSON to {output_path}: {exc}"
            ) from exc

    def _validate_step_output(self, parsed: dict, step_id: str) -> None:
        if not isinstance(parsed, dict):
            raise WorkflowError(f"Step {step_id} output must be a JSON object")

        missing = STEP_RESULT_KEYS - parsed.keys()
        if missing:
            keys = ", ".join(sorted(missing))
            raise WorkflowError(f"Step {step_id} output missing keys: {keys}")

        extra = parsed.keys() - STEP_RESULT_KEYS
        if extra:
            keys = ", ".join(sorted(extra))
            raise WorkflowError(f"Step {step_id} output has unexpected keys: {keys}")

        if parsed["status"] not in {"ok", "fail"}:
            raise WorkflowError(f"Step {step_id} output status must be `ok` or `fail`")

        if not isinstance(parsed["handoff_summary"], str):
            raise WorkflowError(
                f"Step {step_id} output handoff_summary must be a string"
            )

    def _resolve_artifacts_root(self, artifacts_dir: str | None) -> Path:
        if artifacts_dir:
            return Path(artifacts_dir).expanduser().resolve()

        return self.invocation_dir / ".codex-workflow"

    def _build_run_dir(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        run_dir = self.artifacts_root / "runs" / timestamp
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/run_workflow.py",
        description="Run a YAML-defined Codex workflow.",
    )
    parser.add_argument("workflow_file", help="Path to the workflow YAML file")
    parser.add_argument(
        "--schema",
        default="schemas/step-output.schema.json",
        help="Path to the step output schema file",
    )
    parser.add_argument(
        "--codex-command",
        default="codex",
        help="Command used to invoke Codex",
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
        schema_path=args.schema,
        codex_command=args.codex_command,
        artifacts_dir=args.artifacts_dir,
    )
    return runner.run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WorkflowError as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        sys.exit(1)
