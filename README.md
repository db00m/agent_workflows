# Codex Workflow Prototype

This repo contains a Python workflow runner for Codex workflows.

The workflow is defined in YAML. Steps default to interactive sessions backed by
`codex app-server`. Steps can also set `interactive: false` to run once via
`codex exec`.

Interactive steps let you keep talking to the step until you enter one of the
reserved workflow commands:

- `/done`: finalize the current step and move to the next step
- `/retry`: restart the current step from a clean session
- `/fail`: stop the workflow immediately

When a step is finalized, the runner asks Codex to return JSON matching that
step's declared `output` shape. Steps can reference other steps' outputs using
`$ref` inside `input`, and the runner builds execution order from
those dependencies.

Steps with `interactive: false` use the same prompt construction and output
validation, but run once without the interactive `/done` loop.

## Workflow YAML

The workflow file is an ordered YAML document stream. Each document is one
step. Each document must have exactly one top-level key, and that key is the
step name. Execution order is derived from `$ref` dependencies; file order is
only used as a stable tie-break when steps are independent.

```yaml
summarize:
  role: summarizer
  prompt: |
    Summarize the inspection result for the user.
  input:
    valid:
      $ref: inspect.output.valid
    text:
      $ref: inspect.output.result
  output:
    summary: string
---
inspect:
  role: inspector
  model: gpt-5-codex
  interactive: false
  prompt: |
    Inspect the repository and determine whether the workflow is valid.
  output:
    valid: boolean
    result: string
```

Supported structure and fields for each step document:

- top-level key: required unique step name
- `role`: required role name
- `prompt`: required task instructions
- `model`: optional per-step model override
- `interactive`: optional boolean, defaults to `true`
- `input`: optional structured input
- `output`: required output declaration written in YAML

Output declarations support:

- scalar type names: `string`, `boolean`, `number`, `integer`, `null`, `any`
- nested objects via YAML mappings
- arrays via a single-item YAML list, for example `tags: [string]`

## References

References use explicit `$ref` objects inside `input`:

```yaml
input:
  text:
    $ref: inspect.output.result
```

Rules:

- references may target any named step in the workflow
- the stable reference root is `<step_name>.output`
- execution order is determined by these references
- cycles in the dependency graph are rejected
- invalid references are rejected before execution starts

## Prompt And Session Behavior

Each step's initial message is constructed from:

- `role: <role>; task: <prompt>`
- the resolved `input`

Steps with `interactive: true` are told to collaborate with the user until
finalization. Steps with `interactive: false` are told to complete the task in
one pass and return only the final JSON object.

The runner does not resolve `myteam` roles itself. The agent is expected to use
`myteam` directly during the session if it needs role details.

## Artifacts

Artifacts are written under `.codex-workflow/runs/<timestamp>/` in the
directory where you launch the runner.

For each step attempt, the runner stores:

- the initial prompt
- the resolved input
- the reference resolution record
- a human-readable transcript
- the finalized JSON result, when the step completes

Interactive attempts also store:

- app-server protocol traffic
- app-server stderr
- the thread start request and response
- each turn request

Attempts for steps with `interactive: false` also store:

- the exec request payload
- the JSON Schema file passed to `codex exec`
- exec stdout
- exec stderr

Retries create a new `attempt-XX` directory for that step.

## Usage

Run a real workflow:

```bash
python3 scripts/run_workflow.py workflows/example.yaml
```

Test with the local mocks:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --codex-command "python3 scripts/mock_codex_app_server.py" --codex-exec-command "python3 scripts/mock_codex_exec.py"
```

Then interact with any steps that keep `interactive: true` and use `/done` to advance.

You can override the artifacts directory:

```bash
python3 scripts/run_workflow.py workflows/example.yaml --artifacts-dir /tmp/codex-workflow-artifacts
```

## Files

- `scripts/run_workflow.py`: workflow runner
- `scripts/mock_codex_app_server.py`: local app-server mock for interactive verification
- `scripts/mock_codex_exec.py`: local exec mock for `interactive: false` verification
- `workflow_server.md`: design notes for the app-server-based workflow model
- `workflows/example.yaml`: example workflow
