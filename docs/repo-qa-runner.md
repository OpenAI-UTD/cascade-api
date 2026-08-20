# Repository QA Runner

The Repository QA Runner closes the gap between project evidence collection and the Project QA API. It checks out a Git revision into a disposable directory, detects the project stack, reads project documentation, runs an approved plan in constrained Docker containers, submits the results, optionally applies exact documented changes, and reruns the non-install checks to verify the result.

The runner never edits the owning repository. Fixes are applied only inside its disposable clone. Its JSON report retains the resulting Git diff after the checkout is removed.

## Supported detection

| Stack | Markers | Default plan |
|---|---|---|
| Python | `pyproject.toml`, requirements/setup files, or Python files | optional dependency environment, compile, unittest/pytest, optional Ruff |
| Node.js | `package.json` | dependency install plus detected `lint`, `test`, `build`, `qa:runtime`, and `smoke` scripts |
| Go | `go.mod` | module download, vet, tests |
| .NET | solution or project files | restore, build, tests |
| Maven | `pom.xml` | Maven tests |
| Gradle | Gradle build files or wrapper | Gradle tests |

Runtime execution is deliberately explicit. Node projects can expose `qa:runtime` or `smoke`. Other stacks should define a bounded runtime smoke command in `.cascade/qa-runner.json`; the command must start, exercise, and stop the application before its timeout. Long-running servers are rejected by timeout and do not remain running.

## Isolation and approval boundaries

Docker is the default and expected executor. Each command receives:

- a bind mount of only the disposable checkout;
- no host secrets or inherited environment variables;
- no network by default;
- all Linux capabilities dropped and `no-new-privileges` enabled;
- CPU, memory, process, and command time limits;
- a private temporary filesystem;
- an approved stack image and argv list without a command shell.

Dependency commands can use container networking only when the runner is started with `--allow-dependency-network`. Missing images are not pulled unless `--pull-images` is provided. Remote Git URLs require `--allow-remote`, may not embed credentials, and should obtain credentials through the CI provider's normal checkout or credential helper.

Project-defined commands require `--allow-project-commands`. Their executable must match the selected stack allowlist. This approval is consequential: tests and builds execute repository code, even when the command itself is allowlisted, which is why Docker remains mandatory for untrusted projects.

The `local` executor exists only for controlled runner tests. It requires both `--executor local` and `--allow-local-execution` and must not be used on untrusted repositories.

## Project configuration

Copy [the example](../examples/qa-runner.example.json) to `.cascade/qa-runner.json` in the target project. The core fields are:

- `project_id`, `project_name`, and `team`: stable project identity.
- `stacks`: optional stack override; otherwise detection is automatic.
- `documentation`: repository-relative glob patterns.
- `commands`: optional project-approved argv arrays and timeouts.
- `replace_detected_commands`: replace the detected plan instead of appending custom commands; useful when project tooling needs exact orchestration.
- `documented_fixes`: documentation path, failure patterns, recommendation, and exact source replacements.
- `source_files`: additional source snapshots to submit.
- `fail_on`: quality-gate threshold.

Commands are argv arrays, not shell strings. A runtime command looks like:

```json
{
  "name": "Runtime smoke test",
  "stack": "python",
  "kind": "runtime",
  "phase": "runtime",
  "argv": ["python", "scripts/runtime_smoke.py"],
  "timeout_seconds": 120
}
```

## Run and verify

Start the Project QA API, then run:

```powershell
python services/repo-qa-runner/app/main.py C:\projects\payments-api `
  --api-url http://localhost:8040 `
  --allow-project-commands `
  --allow-dependency-network `
  --pull-images `
  --fix-mode apply `
  --result payments-qa-result.json
```

Fix modes are:

- `recommend`: report findings and recommendations without asking for edits.
- `propose`: return documentation-grounded edits but do not apply them.
- `apply`: apply proposals in the clone, rerun build/test/lint/security/runtime commands, submit a verification evaluation, and pass only if the final quality gate passes.

An exit code of `0` means the final quality gate passed. `2` means QA completed but the gate failed. `1` means checkout, policy, execution orchestration, or API communication failed.

The API key is read only from `CASCADE_QA_API_KEY`; do not put it in project configuration or command arguments.

## CI example

```yaml
- name: Run repository QA
  env:
    CASCADE_QA_API_URL: ${{ secrets.CASCADE_QA_API_URL }}
    CASCADE_QA_API_KEY: ${{ secrets.CASCADE_QA_API_KEY }}
  run: |
    python path/to/cascade/services/repo-qa-runner/app/main.py . \
      --allow-project-commands \
      --allow-dependency-network \
      --pull-images \
      --fix-mode apply \
      --result repo-qa-result.json
```

For a normal project pipeline, prefer using the CI provider's already authenticated checkout as the local repository source. The runner clones that checkout again so all execution and edits remain disposable.
