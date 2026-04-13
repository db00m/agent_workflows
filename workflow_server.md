# Server Version

This version of workflows supports both `codex app-server` and `codex exec`.
Interactive steps use `codex app-server`. Steps with `interactive: false` use
`codex exec`.

The workflow is still ordered and step-based. Interactive steps start as
interactive sessions. When the user decides the step is complete, they use
`/done` to end that session and advance to the next step. Steps with
`interactive: false` run once and immediately return their finalized JSON. In
both modes, the completed step must produce JSON that matches the output format
defined for that step in the workflow YAML.

The workflow no longer depends on a shared external output schema file. The only
required external file is the workflow file being executed.

## Commands

The workflow runner reserves these commands during an interactive step:

- `/done`: finalize the current step and advance to the next step
- `/fail`: fail the workflow immediately and stop execution
- `/retry`: discard the current step result and restart the current step

For now, only the user can trigger `/done`. In the future, the agent may be
allowed to decide that a step is ready to finalize, but that is not part of the
current design.

## YAML Format

Workflows should be stored as an ordered YAML document stream. Each YAML
document is one step. Each document must have exactly one top-level key, and
that key is the step name. Execution order should be derived from `$ref`
dependencies, with file order used only as a stable tie-break for independent
steps.

```yaml
summarize:
  role: summarizer
  prompt: Summarize the inspection result for the user.
  input:
    valid:
      $ref: inspect.output.valid
    text:
      $ref: inspect.output.result
  output:
    summary: string
---
inspect:
  model: gpt-5-codex
  role: inspector
  interactive: false
  prompt: Inspect the repository and determine whether the workflow is valid.
  output:
    valid: boolean
    result: string
```

Supported structure and fields for each step document:

- top-level key: required unique step name
- `role`: required role name to provide to the agent
- `prompt`: required task instructions for the step
- `model`: optional model override for the step
- `interactive`: optional boolean; defaults to `true`. When `false`, the step runs via `codex exec`
- `input`: optional structured input injected into the step
- `output`: required JSON output shape for the step, written in YAML

The `output` field defines the JSON object the step must produce when it is
finalized.

## References

References should use explicit `$ref` syntax inside `input`.

```yaml
input:
  text:
    $ref: inspect.output.result
```

Reference rules:

- references may target any named step in the workflow
- references must resolve during workflow validation, or the workflow is invalid
- references are resolved against finalized step output
- the stable reference root is `<step_name>.output`
- execution order is determined by the dependency graph implied by references
- dependency cycles are invalid

Examples:

- `inspect.output.result`
- `plan.output.summary`

This keeps references explicit and machine-validated instead of overloading
plain strings with dotted-path semantics.

## Input

`input` is structured data passed into the current step from the
workflow definition and prior step outputs.

This is separate from `prompt`:

- `prompt` contains the task instructions
- `input` contains structured data made available to the step

Using a dedicated field avoids treating prior step output as a prompt
replacement.

## Prompt Construction

The initial prompt for a step should be constructed in this form:

`role: <role_name>; task: <prompt>`

The runner should also provide the resolved `input` for the step.

Interactive steps should be told to collaborate until the user finalizes the
step. Steps with `interactive: false` should be told to complete the task in
one pass and return only the final JSON object.

The runner should not resolve `myteam` roles itself. Agents should use the
`myteam` CLI directly if they need role details during the session. This keeps
workflow orchestration lighter and gives the agent freedom to use `myteam` in
its intended way.

## Validation

Validation should reject a workflow when:

- step names are duplicated
- a required field is missing
- a step role is missing or empty
- a step output shape is missing or malformed
- a `$ref` target does not exist
- the dependency graph contains a cycle
- a `$ref` path does not exist in the referenced step's declared output shape

Reference validation should happen before workflow execution starts.

## Artifacts

For each step, the runner should store:

- a transcript of the step input and final output
- the finalized JSON result
- the resolved input passed into the step
- the exact prompt or initialization payload used to start the step
- a record of reference resolution for that step

Interactive steps should also store app-server protocol artifacts. Steps with
`interactive: false` should also store the exec invocation, schema file,
stdout, and stderr.

These artifacts are important for debugging because later steps may depend on
specific fields from earlier step outputs.
