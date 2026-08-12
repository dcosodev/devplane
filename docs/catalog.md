# Catalog model

DevPlane catalogs are local, reviewable YAML and Markdown. They are independent from workflow engines and agent runtimes.

## Objects

### CapabilityCatalog

`manifest.yaml` lists capability files and optional reusable profiles:

```yaml
apiVersion: devplane.dev/v1
kind: CapabilityCatalog
metadata:
  name: engineering
spec:
  capabilities:
    - ref: capabilities/base/capability.yaml
    - ref: capabilities/python/capability.yaml
  profiles:
    - id: python-service
      capabilities:
        - base@1.0.0
        - python@2.0.0
```

References must remain inside the catalog root. Duplicate capability or profile IDs are rejected.

### Capability

```yaml
apiVersion: devplane.dev/v1
kind: Capability
metadata:
  id: python
  version: 2.0.0
spec:
  context:
    plan:
      include: [architecture.md]
    implement:
      include: [coding.md, testing.md]
  permissions:
    write: ["src/**", "tests/**"]
    shell:
      allow: ["uv run pytest*", "uv run ruff*"]
  validations:
    - uv run pytest
    - uv run ruff check src tests
```

Markdown resources become prompt context. `permissions` are declarative policy and generated context; they are not an OS sandbox. `validations` are trusted executable contract data when used to create an execution plan.

### AgentProject

A project selects one profile and may add capabilities:

```yaml
apiVersion: devplane.dev/v1
kind: AgentProject
metadata:
  name: service
spec:
  catalog:
    source: ../engineering-catalog
  profile: python-service
  capabilities:
    - observability@1.3.0
```

`workflow` and `runtime` are optional. This is a valid catalog-only project.

## Resolution

`devplane sync`:

1. contains catalog and resource paths;
2. loads YAML with `safe_load`;
3. expands the selected profile;
4. appends direct capabilities and removes exact duplicate requests;
5. verifies requested versions;
6. merges context, scopes, shell patterns, and validations deterministically;
7. writes `.devplane/generated/resolved-manifest.yaml` and context bundles;
8. hashes project configuration, catalog manifests, capability files, and included resources.

`devplane sync --check` and `devplane validate` fail on generated drift.

## Composition rules

- A project selects at most one profile.
- Direct capabilities extend the profile.
- Every requested capability may pin a version with `id@version`; public examples always pin.
- The current local catalog contains one version of each capability ID.
- Validations preserve profile/capability order while removing exact duplicates.
- Context resources are sorted by logical path; write and shell patterns are sorted and deduplicated.
- Automatic framework-to-profile activation is intentionally absent. `devplane profile` discovers evidence; `devplane use-profile` records the human decision.

## Trust boundary

Catalogs are executable configuration even when most files are Markdown:

- instructions can contain prompt injection;
- validation commands execute later during governed plans;
- path and shell patterns do not enforce syscall isolation;
- catalogs have source hashes but no signatures;
- local catalogs can change between review and resolution.

Review catalog changes like source code and pin the catalog repository revision in your own deployment process.
