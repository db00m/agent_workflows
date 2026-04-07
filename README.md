# Codex Workflow Prototype

This repo contains an interactive Python workflow runner for `codex app-server`.

The workflow is defined in YAML. Each step starts an interactive Codex session.
You can keep talking to the step until you enter one of the reserved workflow
commands:

- `/done`: finalize the current step and move to the next step
- `/retry`: restart the current step from a clean session
- `/fail`: stop the workflow immediately

When a step is finalized, the runner asks Codex to return JSON matching that
step's declared `output` shape. Later steps can reference fields from earlier
outputs using `$ref` inside `additional_context`.

## Workflow YAML

The top-level format is:

```yaml
version: 2
steps:
  - id: inspect
    role: inspector
    model: gpt-5-codex
    prompt: |
      Inspect the repository and determine whether the workflow is valid.
    output:
      valid: boolean
      result: string

  - id: summarize
    role: summarizer
    prompt: |
      Summarize the inspection result for the user.
    additional_context:
      valid:
        $ref: steps.inspect.output.valid
      text:
        $ref: steps.inspect.output.result
    output:
      summary: string
```

Supported fields:

- `version`: currently `2`
- `steps`: ordered list of steps
- `steps[].id`: required unique step identifier
- `steps[].role`: required role name
- `steps[].prompt`: required task instructions
- `steps[].model`: optional per-step model override
- `steps[].additional_context`: optional structured context
- `steps[].output`: required output declaration written in YAML

Output declarations support:

- scalar type names: `string`, `boolean`, `number`, `integer`, `null`, `any`
- nested objects via YAML mappings
- arrays via a single-item YAML list, for example `tags: [string]`

## References

References use explicit `$ref` objects:

```yaml
additional_context:
  text:
    $ref: steps.inspect.output.result
```

Rules:

- references may only target earlier steps
- the stable reference root is `steps.<step_id>.output`
- invalid references are rejected before execution starts

## Prompt And Session Behavior

Each step's initial message is constructed from:

- `role: <role>; task: <prompt>`
- the resolved `additional_context`

The runner does not resolve `myteam` roles itself. The agent is expected to use
`myteam` directly during the interactive session if it needs role details.

## Artifacts

Artifacts are written under `.codex-workflow/runs/<timestamp>/` in the
directory where you launch the runner.

For each step attempt, the runner stores:

- app-server protocol traffic
- app-server stderr
- the thread start request and response
- each turn request
- the initial prompt
- the resolved additional context
- the reference resolution record
- a human-readable transcript
- the finalized JSON result, when the step completes

Retries create a new `attempt-XX` directory for that step.

## Usage

Run a real workflow:

```bash
python3 scripts/run_workflow.py workflows/example.yaml
```

Test with the local mock app server:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --codex-command "python3 scripts/mock_codex_app_server.py"
```

Then interact with each step and use `/done` to advance.

You can override the artifacts directory:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --artifacts-dir /tmp/codex-workflow-artifacts
```

## Files

- `scripts/run_workflow.py`: workflow runner
- `scripts/mock_codex_app_server.py`: local app-server mock for verification
- `workflow_server.md`: design notes for the app-server-based workflow model
- `workflows/example.yaml`: example workflow
