# Project QA Service

This FastAPI service accepts evidence from any project's CI pipeline and runtime smoke tests. It returns normalized findings, recommendations, a quality-gate decision, and optionally conservative source change proposals backed by project documentation.

Run locally from the repository root:

```powershell
python -m uvicorn --app-dir services/project-qa-service app.main:app --host 0.0.0.0 --port 8040
```

OpenAPI is available at `http://localhost:8040/docs`. See `docs/project-qa-api.md` for the request contract and CI integration.
