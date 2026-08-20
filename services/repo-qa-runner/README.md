# Repository QA Runner

The runner checks out a Git project into a disposable workspace, detects supported stacks, reads project documentation, executes an approved QA plan, submits evidence to the Project QA API, and can apply and verify exact documentation-grounded proposals.

Project commands use Docker isolation by default. The runner itself runs on the CI host so the Docker daemon can bind-mount the disposable checkout without exposing the owning repository.

```powershell
python services/repo-qa-runner/app/main.py C:\path\to\project `
  --api-url http://localhost:8040 `
  --allow-project-commands `
  --allow-dependency-network `
  --pull-images `
  --fix-mode apply
```

See [the runner guide](../../docs/repo-qa-runner.md) and [example configuration](../../examples/qa-runner.example.json).
