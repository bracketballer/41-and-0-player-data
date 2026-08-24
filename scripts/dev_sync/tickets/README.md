# Ticketed release handlers

Add one reviewed Python module per schema-plus-data ticket, named
`issue_<zero-padded-ticket-number>_<slug>.py`. The module must expose:

```python
def validate_artifact(root: Path, descriptor: dict) -> dict:
    """Validate files without opening a write transaction."""

def apply(conn, root: Path, descriptor: dict, prepared: dict) -> dict:
    """Write idempotently; return row_counts and validation dictionaries."""
```

`apply` receives a transaction-owned PostgreSQL connection and must not commit
or change the active Fastify checkout. Use a new release version for a retry;
published audit rows are immutable.
