# Codex Workflow Prototype

This repo contains a small Python workflow runner for `codex exec`.

The workflow is defined in YAML. Each step runs in order. A step must return a
structured response that matches the shared contract below:

```json
{
  "status": "ok | fail",
  "handoff_summary": "short summary for the next step"
}
```

If any step returns `status: "fail"`, the runner stops immediately and exits
non-zero.

The workflow file can live anywhere, but each `codex exec` step runs in the
directory where you launch the runner.

## Workflow YAML

The top-level format is:

```yaml
version: 1
defaults:
  sandbox: workspace-write
steps:
  - id: inspect
    role: analyst
    prompt: |
      Inspect the repository and summarize the current project structure.

      If the repository is empty, say so in the handoff summary and return
      status "ok".
  - id: plan
    role: planner
    prompt: |
      Propose an implementation plan for a workflow runner that executes
      sequential Codex steps from YAML.

      Return status "fail" if the repository state prevents meaningful work.
  - id: implement
    role: implementer
    prompt: |
      Implement the planned workflow runner.

      Return status "fail" if the implementation cannot be completed safely.
```

Supported fields:

- `version`: currently `1`
- `defaults`: optional mapping of step defaults
- `defaults.model`: optional `codex exec --model`
- `defaults.sandbox`: optional `codex exec --sandbox`
- `defaults.profile`: optional `codex exec --profile`
- `steps`: ordered list of steps
- `steps[].id`: required unique step identifier
- `steps[].role`: optional role name resolved with `myteam get role --role NAME`
- `steps[].prompt`: required instructions for that step
- `steps[].model`: optional per-step model override
- `steps[].sandbox`: optional per-step sandbox override
- `steps[].profile`: optional per-step profile override

## Handoff Behavior

Before each step, the runner wraps the step prompt with:

- the workflow step id
- the resolved `myteam` role instructions when `role` is present
- the previous step's `handoff_summary` when present
- the required output contract

That gives each agent a clean handoff channel without exposing workflow control
to ad hoc tool behavior.

## Files

- `scripts/run_workflow.py`: workflow runner
- `schemas/step-output.schema.json`: shared step output contract
- `workflows/example.yaml`: example workflow
- `scripts/mock_codex_exec.sh`: local test shim for verification
- `scripts/mock_myteam.sh`: local `myteam` shim for verification

## Usage

Run a real workflow:

```bash
python3 scripts/run_workflow.py workflows/example.yaml
```

That command reads `workflows/example.yaml`, but the workflow itself runs in the
current shell directory.

Choose a different schema file:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --schema schemas/step-output.schema.json
```

Test the runner without calling the live Codex API:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --codex-command ./scripts/mock_codex_exec.sh --myteam-command ./scripts/mock_myteam.sh
```

By default, artifacts are written under `.codex-workflow/runs/<timestamp>/` in
the directory where you launch the runner, not next to the workflow file. This
keeps run state out of the Codex workspace being modified by the workflow.

You can override that location:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --artifacts-dir /tmp/codex-workflow-artifacts
```

For each step, the runner stores:

- the resolved role instructions, if the step defines `role`
- the fully constructed prompt
- stdout and stderr
- the parsed JSON result
- the exact command used
