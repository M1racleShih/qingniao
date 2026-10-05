# Examples

## Shared configuration sample

`qingniao-config.example.json` shows the complete shared configuration
schema in format version 1:

- `schema_version` pins the format (unknown future versions are rejected,
  never migrated); `generation` increases with every persisted write and
  `last_operation` records the import transaction that last committed the
  file, when any.
- `providers` map a provider ID to an Anthropic-compatible API root
  (`base_url`, loopback `http` fixtures for development, `https` for real
  endpoints), **exactly one** credential source — the name of the
  environment variable that holds the credential (`credential_env`), a
  credential catalog id, or a private credential id (`credential_id`,
  created by `qing config import-claude` or `qing credential add
  --from-stdin`) — and the header style (`auth`: `bearer` or `x-api-key`).
- `credentials` (optional, additive) names catalog credential entries:
  `{"source": "env", "env": NAME}` reads the credential from the gateway
  process environment variable `NAME`; `{"source": "private"}` refers to
  an immutable private store version. An absent section behaves as empty,
  and a provider may reference a catalog id or a direct `cred_<hex>` id.
- `models` map a catalog model ID to a provider and the exact upstream model
  string sent to that provider.
- `defaults` name the default main and auxiliary request models and the
  default route table (`routes` maps the exact request model string a client
  sends to a catalog model ID).

Credential values never live in this file. Providers with
`credential_env` are read from the gateway process's own environment at
request time, so the referenced variables (for example
`QING_DEV_FIXTURE_TOKEN`) must be exported in the shell that starts `qing
serve`; exporting them later in another shell does not update a running
gateway. Providers with `credential_id` read an immutable 0600 file under
`credentials/` in the state directory — created by `qing config
import-claude` or `qing credential add --from-stdin`, readable across
gateway restarts, and a plain local file store, not an encrypted vault.
The `credentials` catalog section can be managed entry-by-entry with
`qing credential` (add/list/show/rm).

Apply it with:

```
QING_DEV_FIXTURE_TOKEN=... qing serve &
qing config apply examples/qingniao-config.example.json
```

No provider endpoint in this example is a claim about real-provider
compatibility; `dev-fixture` expects a local loopback fixture you control.
