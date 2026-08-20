# Project QA API

The `dev` branch introduces a project-agnostic API path for Cascade. A team's CI job sends the results of tests, linting, builds, security checks, and runtime smoke tests to one service. The service normalizes issues, ranks project documentation, returns recommendations, and can propose narrowly scoped changes defined by that documentation.

This path does not require the target project to run in Kubernetes. Existing Kubernetes reliability modules remain available while this API becomes the integration boundary for development teams.

## Run it

From the Cascade repository root:

```powershell
python -m pip install -r services/project-qa-service/requirements.txt
python -m uvicorn --app-dir services/project-qa-service app.main:app --host 0.0.0.0 --port 8040
```

The service exposes:

- `POST /evaluations`: submit CI and runtime evidence.
- `GET /evaluations/{evaluation_id}`: retrieve one result.
- `GET /projects/{project_id}/evaluations`: list recent runs for a project.
- `GET /health`, `GET /ready`, and `/docs`: operations and OpenAPI endpoints.

When deployed with the existing platform, the Command Center proxy exposes the same resource at `POST /api/qa/evaluations`.

## CI flow

```text
project checkout
  -> test, lint, build, scan
  -> deploy an ephemeral test environment
  -> run smoke tests and capture runtime observations
  -> POST one evaluation payload
  -> publish recommendations and quality-gate result
  -> optionally apply exact documentation-grounded proposals
  -> rerun the project's checks
```

The API itself does not clone repositories or execute submitted commands. Use the [Repository QA Runner](repo-qa-runner.md) in CI to perform disposable checkout, stack detection, constrained Docker execution, evidence submission, documented fix application, and verification reruns. This keeps repository credentials and arbitrary project code out of the API service.

## Submit an evaluation

The example payload demonstrates CI checks, a runtime observation, project documentation, and a documented correction rule:

```powershell
./scripts/invoke-project-qa.ps1 `
  -PayloadPath examples/project-qa-evaluation.json `
  -ApiUrl http://localhost:8040 `
  -ResultPath qa-result.json `
  -FailOnGate
```

For a project pipeline, store the API URL and key in CI secrets as `CASCADE_QA_API_URL` and `CASCADE_QA_API_KEY`. Build the JSON payload from that project's test and smoke-test steps, then call the same script.

`fail_on` controls the gate threshold: `info`, `low`, `medium`, `high`, or `critical`. A failed gate is included in the normal API response so pipelines can still publish the complete report. The helper exits with code `2` only when `-FailOnGate` is supplied.

## Recommendations and proposed fixes

`mode: recommend` returns findings and next steps but never proposes source edits.

`mode: propose_fix` may return a proposed change only when all of these checks pass:

1. A submitted issue contains a configured `issue_patterns` value.
2. The rule's `documentation_path` is included in the request.
3. The target source file snapshot is included in the request.
4. The rule's `original` text occurs exactly once in that snapshot.

When any condition fails, the response records a `withheld_fixes` reason. The service never invents an ungrounded source edit.

To apply returned proposals inside a disposable CI checkout:

```powershell
./scripts/invoke-project-qa.ps1 `
  -PayloadPath qa-request.json `
  -ResultPath qa-result.json `
  -ProjectRoot . `
  -ApplyProposals
```

The helper repeats path containment and exact-match checks before editing. The pipeline should then rerun all checks, show the diff, and use its normal review/commit policy; applying a proposal is not proof that the fix is correct.

## Multi-project behavior

Every request includes a stable `project.project_id`, branch, revision, and pipeline reference. Results are isolated and listed by project ID. The current MVP stores the latest 100 evaluations per project in service memory; production use should replace this store with durable, tenant-aware storage and enable API authentication at the ingress or Command Center proxy.
