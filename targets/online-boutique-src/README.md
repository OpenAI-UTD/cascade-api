# Online Boutique (legacy target source, fetched on demand)

This directory is intentionally **empty in the repository**. It previously held a
git-submodule-style snapshot (`mode 160000` gitlink) of the legacy Google
Online Boutique demo workload, but had no `.gitmodules` entry — fresh clones
got an empty directory with no way to populate it.

Online Boutique is **inactive** for Cascade. The canonical observed demo target
is Sock Shop under `targets/sock-shop/`. Only fetch this source for historical
comparison or deliberate legacy compatibility testing.

## Fetching the source (optional)

```bash
git clone --depth 1 https://github.com/GoogleCloudPlatform/microservices-demo.git /tmp/online-boutique
mkdir -p targets/online-boutique-src
cp -R /tmp/online-boutique/src/. targets/online-boutique-src/
rm -rf /tmp/online-boutique
```

- Upstream: `https://github.com/GoogleCloudPlatform/microservices-demo`
- Copy only the `src/` subtree; build outputs (`**/build/`, `**/.gradle/`) stay
  untracked via `.gitignore`.

## Guardrails

- Do not reference this target in demos, default policies, or acceptance
  scripts; the active-target catalog and lint tests forbid it
  (`tests/test_target_workload_catalog.py`).
- Do not commit fetched sources back into this repository.
- See `docs/legacy-online-boutique.md` for the full policy.
