from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.run_workflow import WorkflowError, WorkflowRunner


class WorkflowRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.schema_path = self.root / "step-output.schema.json"
        self.schema_path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["ok", "fail"]},
                        "handoff_summary": {"type": "string"},
                    },
                    "required": ["status", "handoff_summary"],
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
        self.mock_path = self.root / "mock_codex_exec.py"
        self.mock_path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                output_file = None

                while args:
                    current = args.pop(0)
                    if current in {"-o", "--output-last-message"}:
                        output_file = args.pop(0)

                if output_file is None:
                    print("missing output file", file=sys.stderr)
                    sys.exit(1)

                prompt = sys.stdin.read()
                if "workflow step `step-two`" in prompt and "summary from step-one" not in prompt:
                    print("handoff missing from step-two prompt", file=sys.stderr)
                    sys.exit(1)

                status = "ok"
                summary = "generic summary"
                payload = {"status": status, "handoff_summary": summary}
                if "workflow step `step-one`" in prompt:
                    payload = {"status": "ok", "handoff_summary": "summary from step-one"}
                elif "workflow step `step-two`" in prompt:
                    payload = {"status": "ok", "handoff_summary": "summary from step-two"}
                elif "workflow step `step-fail`" in prompt:
                    payload = {"status": "fail", "handoff_summary": "step-fail requested stop"}
                elif "workflow step `step-never`" in prompt:
                    payload = {"status": "ok", "handoff_summary": "step-never should not run"}
                elif "workflow step `step-bad-status`" in prompt:
                    payload = {"status": "maybe", "handoff_summary": "bad status payload"}
                elif "workflow step `step-extra-field`" in prompt:
                    payload = {
                        "status": "ok",
                        "handoff_summary": "extra field payload",
                        "extra": "not allowed",
                    }

                Path(output_file).write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
                print(f"stdout for {payload['handoff_summary']}")
                """
            ),
            encoding="utf-8",
        )
        self.mock_command = f"{shutil.which('python3') or 'python3'} {self.mock_path}"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        shutil.rmtree(Path.cwd() / ".codex-workflow", ignore_errors=True)

    def test_load_workflow_parses_typed_steps(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            defaults:
              model: gpt-5.2
              sandbox: workspace-write
            steps:
              - id: step-one
                prompt: First prompt
              - id: step-two
                profile: ci
                prompt: Second prompt
            """
        )

        runner = WorkflowRunner(
            workflow_path=str(workflow_path),
            schema_path=str(self.schema_path),
            codex_command=self.mock_command,
        )

        workflow = runner.load_workflow()

        self.assertEqual(workflow.version, 1)
        self.assertEqual(workflow.defaults.model, "gpt-5.2")
        self.assertEqual(workflow.defaults.sandbox, "workspace-write")
        self.assertEqual([step.id for step in workflow.steps], ["step-one", "step-two"])
        self.assertEqual(workflow.steps[1].profile, "ci")

    def test_runner_executes_steps_in_order_and_propagates_handoff(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            defaults:
              sandbox: workspace-write
            steps:
              - id: step-one
                prompt: First step
              - id: step-two
                prompt: Second step
            """
        )

        runner = WorkflowRunner(
            workflow_path=str(workflow_path),
            schema_path=str(self.schema_path),
            codex_command=self.mock_command,
        )

        exit_code = runner.run()

        self.assertEqual(exit_code, 0)
        first_step_dir = runner.run_dir / "01-step-one"
        second_step_dir = runner.run_dir / "02-step-two"
        self.assertTrue((first_step_dir / "prompt.txt").is_file())
        self.assertTrue((first_step_dir / "stdout.txt").is_file())
        self.assertTrue((first_step_dir / "stderr.txt").is_file())
        self.assertTrue((first_step_dir / "result.json").is_file())
        self.assertTrue((first_step_dir / "command.txt").is_file())
        self.assertTrue((second_step_dir / "prompt.txt").is_file())

        second_prompt = (second_step_dir / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Previous handoff summary:", second_prompt)
        self.assertIn("summary from step-one", second_prompt)

        step_two_result = json.loads((second_step_dir / "result.json").read_text())
        self.assertEqual(step_two_result["handoff_summary"], "summary from step-two")

    def test_runner_stops_after_failed_step(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            steps:
              - id: step-one
                prompt: First step
              - id: step-fail
                prompt: Fail this step
              - id: step-never
                prompt: This should not execute
            """
        )

        runner = WorkflowRunner(
            workflow_path=str(workflow_path),
            schema_path=str(self.schema_path),
            codex_command=self.mock_command,
        )

        exit_code = runner.run()

        self.assertEqual(exit_code, 1)
        self.assertTrue((runner.run_dir / "01-step-one" / "result.json").is_file())
        self.assertTrue((runner.run_dir / "02-step-fail" / "result.json").is_file())
        self.assertFalse((runner.run_dir / "03-step-never").exists())

    def test_validation_rejects_duplicate_step_ids(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            steps:
              - id: duplicate
                prompt: One
              - id: duplicate
                prompt: Two
            """
        )

        runner = WorkflowRunner(
            workflow_path=str(workflow_path),
            schema_path=str(self.schema_path),
            codex_command=self.mock_command,
        )

        with self.assertRaises(WorkflowError):
            runner.load_workflow()

    def test_runner_rejects_output_that_violates_schema(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            steps:
              - id: step-bad-status
                prompt: Produce an invalid status
            """
        )

        runner = WorkflowRunner(
            workflow_path=str(workflow_path),
            schema_path=str(self.schema_path),
            codex_command=self.mock_command,
        )

        with self.assertRaises(WorkflowError) as context:
            runner.run()

        self.assertIn("`status` must be one of `ok`, `fail`", str(context.exception))

    def test_runner_rejects_unexpected_output_fields(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            steps:
              - id: step-extra-field
                prompt: Produce an invalid extra field
            """
        )

        runner = WorkflowRunner(
            workflow_path=str(workflow_path),
            schema_path=str(self.schema_path),
            codex_command=self.mock_command,
        )

        with self.assertRaises(WorkflowError) as context:
            runner.run()

        self.assertIn("unexpected keys: extra", str(context.exception))

    def test_cli_runs_with_default_schema(self) -> None:
        workflow_path = self._write_workflow(
            """
            version: 1
            steps:
              - id: step-one
                prompt: First step
            """
        )

        command = [
            shutil.which("python3") or "python3",
            str(Path.cwd() / "scripts" / "run_workflow.py"),
            str(workflow_path),
            "--codex-command",
            self.mock_command,
        ]

        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            cwd=Path.cwd(),
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Workflow completed successfully.", completed.stdout)

    def _write_workflow(self, contents: str) -> Path:
        workflow_path = self.root / "workflow.yaml"
        workflow_path.write_text(textwrap.dedent(contents).strip() + "\n", encoding="utf-8")
        return workflow_path


if __name__ == "__main__":
    unittest.main()
