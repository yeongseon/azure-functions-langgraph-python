# Production auth example

Demonstrates per-graph host `auth_level` overrides, not per-user thread ownership.
The anonymous demo agent is **local-only**; remove it before production deployment.
The health endpoint in this example is protected.

## Auth model

The app uses `auth_level=AuthLevel.FUNCTION` as the default (so all endpoints require a function key), with `health_auth_level=AuthLevel.FUNCTION` to also protect the health endpoint. Individual graphs **override** the default:

| Endpoint | Auth | Why |
| --- | --- | --- |
| `GET /api/health` | FUNCTION | Protected; set `health_auth_level=ANONYMOUS` to expose |
| `POST /api/graphs/public_agent/{invoke,stream}` | ANONYMOUS | Demo / public surface |
| `POST /api/graphs/private_agent/{invoke,stream}` | FUNCTION | Requires `?code=<FUNCTION_KEY>` |

> The minimal liveness probe can deliberately be anonymous. Keep the application
> FUNCTION default for production; a key is not user/tenant authorization. Review
> [the security route boundaries](../../docs/security.md) before using stateful agents.

## Files

- `function_app.py` — registers both graphs with explicit per-graph `auth_level`
- `graph.py` — two minimal echo graphs
- `host.json`, `local.settings.json.example`, `requirements.txt`
- `verify.sh` — curl script hitting both protected and unprotected endpoints

## Run locally

```bash
cd examples/production_auth
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

Core Tools does **not** enforce function keys locally. The key-denial checks below
must target a deployed Azure host; local success without a key is not a security
failure and cannot prove deployed protection.

## Verify

```bash
# Health: requires a key (health_auth_level=FUNCTION)
curl -s "http://localhost:7071/api/health?code=$FUNCTION_KEY"

# Public agent: works without a key (per-graph ANONYMOUS override)
curl -s -X POST http://localhost:7071/api/graphs/public_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"hi"}]}}'

# Function-level: 401 without a key
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://localhost:7071/api/graphs/private_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"hi"}]}}'

# Function-level: 200 with the key
curl -s -X POST "http://localhost:7071/api/graphs/private_agent/invoke?code=$FUNCTION_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"human","content":"hi"}]}}'
```

Or run the bundled script:

```bash
BASE="https://<app>.azurewebsites.net/api" FUNCTION_KEY="<function-key>" ./verify.sh
```

## Production deployment notes

- Never commit `local.settings.json` — it is gitignored by Functions Core Tools, but double-check.
- In Azure, function keys live in the Function App's *Functions → App keys / Function keys* blade. Rotate them on a schedule (see [docs/production-guide.md](../../docs/production-guide.md#connection-string-security) for the broader credential hygiene policy).
- For richer auth (Azure AD, custom JWT, IP allowlist), front the Function App with API Management or App Service Authentication ("Easy Auth"); function keys are a coarse-grained primitive.
