# Server Version

This version of workflows uses `codex app-server` instead of `codex exec` so
each workflow step can run as an interactive Codex session.

The workflow is still ordered and step-based. The runner starts the first step
as an interactive session. When the user decides the step is complete, they use
`/done` to end that session and advance to the next step. The completed step
must then produce JSON that matches the output format defined for that step in
the workflow YAML.

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

Workflows should keep the ordered `steps` list instead of using top-level
unordered step mappings.

```yaml
version: 2
steps:
  - id: inspect
    model: gpt-5-codex
    role: inspector
    prompt: Inspect the repository and determine whether the workflow is valid.
    output:
      valid: boolean
      result: string

  - id: summarize
    role: summarizer
    prompt: Summarize the inspection result for the user.
    additional_context:
      valid:
        $ref: steps.inspect.output.valid
      text:
        $ref: steps.inspect.output.result
    output:
      summary: string
```

Supported fields:

- `version`: workflow format version
- `steps`: ordered list of workflow steps
- `steps[].id`: required unique step identifier
- `steps[].role`: required role name to provide to the agent
- `steps[].prompt`: required task instructions for the step
- `steps[].model`: optional model override for the step
- `steps[].additional_context`: optional structured context injected into the step
- `steps[].output`: required JSON output shape for the step, written in YAML

The `output` field defines the JSON object the step must produce when it is
finalized.

## References

References should use explicit `$ref` syntax inside the YAML.

```yaml
additional_context:
  text:
    $ref: steps.inspect.output.result
```

Reference rules:

- references may only target steps that appear earlier in the workflow
- references must resolve during workflow validation, or the workflow is invalid
- references are resolved against finalized step output
- the stable reference root is `steps.<step_id>.output`

Examples:

- `steps.inspect.output.result`
- `steps.plan.output.summary`

This keeps references explicit and machine-validated instead of overloading
plain strings with dotted-path semantics.

## Additional Context

`additional_context` is structured data passed into the current step from the
workflow definition and prior step outputs.

This is separate from `prompt`:

- `prompt` contains the task instructions
- `additional_context` contains structured data made available to the step

Using a dedicated field avoids treating prior step output as a prompt
replacement.

## Prompt Construction

The initial prompt for a step should be constructed in this form:

`role: <role_name>; task: <prompt>`

The runner should also provide the resolved `additional_context` for the step.

The runner should not resolve `myteam` roles itself. Agents should use the
`myteam` CLI directly if they need role details during the session. This keeps
workflow orchestration lighter and gives the agent freedom to use `myteam` in
its intended way.

## Validation

Validation should reject a workflow when:

- step ids are duplicated
- a required field is missing
- a step role is missing or empty
- a step output shape is missing or malformed
- a `$ref` target does not exist
- a `$ref` points to a later step
- a `$ref` path does not exist in the referenced step's declared output shape

Reference validation should happen before workflow execution starts.

## Artifacts

For each step, the runner should store:

- the raw interactive transcript
- the finalized JSON result
- the resolved additional context passed into the step
- the exact prompt or initialization payload used to start the step
- a record of reference resolution for that step

These artifacts are important for debugging because later steps may depend on
specific fields from earlier step outputs.
