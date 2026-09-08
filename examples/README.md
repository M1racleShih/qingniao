# Examples

## Shared configuration sample

`qingniao-config.example.json` shows the complete shared configuration schema:

- `providers` map a provider ID to an Anthropic-compatible API root
  (`base_url`, loopback `http` fixtures for development, `https` for real
  endpoints), the name of the environment variable that holds the credential
  (`credential_env`), and the header style (`auth`: `bearer` or `x-api-key`).
- `models` map a catalog model ID to a provider and the exact upstream model
  string sent to that provider.
- `defaults` name the default main and auxiliary request models and the
  default route table (`routes` maps the exact request model string a client
  sends to a catalog model ID).

Credential values never live in this file. The gateway process reads them
from its own environment at request time, so the referenced variables (for
example `QING_DEV_FIXTURE_TOKEN`) must be exported in the shell that starts
`qing serve`; exporting them later in another shell does not update a running
gateway.

Apply it with:

```
QING_DEV_FIXTURE_TOKEN=... qing serve &
qing config apply examples/qingniao-config.example.json
```

No provider endpoint in this example is a claim about real-provider
compatibility; `dev-fixture` expects a local loopback fixture you control.
