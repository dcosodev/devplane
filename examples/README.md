# Examples

The `catalog/` directory is a minimal DevPlane capability catalog used by the documentation and tests.

It demonstrates:

- the `devplane.dev/v1` catalog manifest;
- an exactly versioned capability;
- context instructions kept as catalog data;
- deterministic capability resolution.

Use it for the public quick start:

```bash
uv run devplane new ../demo-project --catalog ./examples/catalog
```

The example capability is intentionally generic. Replace it with reviewed project conventions rather than treating `sample-base` as a production standard. Its write and shell patterns are illustrative contract data; they do not provide operating-system isolation.

Catalog Markdown becomes model prompt material. Only use catalogs you trust, review changes before synchronization, and keep executable validation commands outside untrusted catalog content.
