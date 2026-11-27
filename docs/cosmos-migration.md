# Cosmos credential migration and decision record (#1034)

## Status and scope

The existing `create_cosmos_checkpointer` behavior and both deprecations remain
unchanged. **Proposal for maintainer review:** retain both APIs without choosing
retirement versions until their respective prerequisites below are met. This
record is not approval to remove either API, raise the dependency floor, or
replace the storage provider. No real Azure operation or real key was used.

The audit of `origin/main` at `153bba6` found startup-environment guidance and
synthetic tests for environment restoration, key precedence, alias rejection,
idempotent close and unrelated-thread visibility already present. Missing were
the complete upstream version inventory, the unusable factory distinction,
caller-owned cleanup, and separate retirement conditions. The issue's
[2026-11-25 investigation](https://github.com/yeongseon/azure-functions-langgraph-python/issues/1034)
is prior evidence, not a maintainer support decision.

## Upstream versions evidence (2026-11-28)

The declared extra is `langgraph-checkpoint-cosmosdb>=0.2.0,<0.5`.
[PyPI's release inventory](https://pypi.org/pypi/langgraph-checkpoint-cosmosdb/json)
contains ten non-yanked releases in this range; 0.2.0–0.2.2 have no published
files. Latest is **0.4.0**, uploaded 2026-09-21. “Supported” here means allowed
by this repository's constraint, not an upstream maintenance promise.

All ten wheels were downloaded with Python 3.11 in an isolated venv using
`python -m pip download --no-deps --only-binary=:all:` with each exact version.
Inspection of `langgraph_checkpoint_cosmosdb/cosmosdbSaver.py` in each wheel
(constructor, `from_conn_info`, environment reads and resource methods) found:

| Versions inspected | Constructor parameters besides `self` | Identity setup |
| --- | --- | --- |
| 0.2.3, 0.2.4, 0.2.5 | `database_name`, `container_name` | Gets existing handles, then also calls create-container |
| 0.2.6 | Above plus `reducer=None`, `messages_key="messages"` | Same additional create-container call |
| 0.2.7, 0.2.8, 0.3.1, 0.3.2, 0.3.3, 0.4.0 | Above plus `reducer=None`, `messages_key="messages"` | Existing database/container handles only |

Every constructor reads `COSMOSDB_ENDPOINT` and `COSMOSDB_KEY`. None accepts
an endpoint, key, credential or client, and none supplies a saver `close()` or
context-manager lifecycle. Key auth creates the database/container if absent.
Identity auth constructs `DefaultAzureCredential` internally.

**`from_conn_info` is not supported injection in practice:** although its
signature advertises endpoint and key, every inspected implementation calls
`CosmosDBSaver(endpoint, key, database_name, container_name)` positionally. In
0.2.3–0.2.5 this exceeds the constructor arity. In 0.2.6–0.4.0 the values bind
to database, container, reducer and messages key instead; authentication still
reads the environment. Its `finally` does no cleanup. Do not migrate to it.

Wheel SHA-256 digests (versioned PyPI artifacts, not mutable `main` source):

```text
0.2.3  2c9e668b56bd71403a774c90c8e0de4e7e11f138ec78769b33fbbf665858b314
0.2.4  fbad4a99c3f5a6bc3aad7071b240b5041a4b49c346f2a7440b90cdc632efb960
0.2.5  abc148262cc64909aca7b832f811d0f4077bed464e6c4ead4ca336dfdf9be477
0.2.6  57b329d014b14c30e4ff72ad002de492bc0a6948b07d465e019c4ee70161139b
0.2.7  4312b43f9bbf1d4a8976e4ab267a056e63b7bc78a21bcad186897a44d476b1d3
0.2.8  2363bd3561f3afb815e8f81e22adc0ba4607757283bd1c4f78efc175488fa42a
0.3.1  2bacbf34b0fac3d5038bba6c2b8b7a334d0e213f008c5cc0b118bf7399fe19f2
0.3.2  c4baf8afd5368484b049145ee8654ec9ce203a5559f5d28c7a353d45532292d5
0.3.3  4251f8e5584faedcab7489e4b3779eee6746e14e6a686d036a61efc6a9e28e06
0.4.0  39a25bfa1aead3dd3819c434ca7da7bb42d35930f2304d66ba3dd3ab9918e680
```

This is artifact/API inspection, not ten-version runtime certification or a
minimum-dependency test. The current
[upstream documentation](https://pypi.org/project/langgraph-checkpoint-cosmosdb/0.4.0/)
agrees with startup environment auth and pre-existing identity resources.
There is **no justified injection floor** within the allowed range, so this
change does not adopt injection or modify the dependency constraint.

## Caller-managed migration (one account per worker)

This optional migration removes temporary environment writes, **not**
process-wide access to credentials. Set application settings before starting
the worker, never temporarily in Python around construction:

| Caller | Startup settings | Prerequisites |
| --- | --- | --- |
| Key-based | `COSMOSDB_ENDPOINT` and `COSMOSDB_KEY` | Securely provision the key; keep the same database/container and `/partition_key` |
| Identity-based | `COSMOSDB_ENDPOINT`; omit `COSMOSDB_KEY` entirely | Pre-provision database/container with `/partition_key`; assign Cosmos data-plane permissions to the selected principal |

For the existing compatibility example, map `AZURE_COSMOS_ENDPOINT` to
`COSMOSDB_ENDPOINT` and `COSMOS_KEY` to `COSMOSDB_KEY` **in deployment settings**,
then replace the helper call with the direct constructor. The upstream saver
does not read the helper's `COSMOS_KEY`. For identity auth, do not pass
`credential=` to the helper: it accepts only a string account key and rejects
identity objects. Prefer testing a pinned **0.4.0** candidate (or at least the
already documented **0.2.8** line) for identity migration; 0.2.7 is the observed
setup-change boundary, not a new support floor. Test dependency compatibility
and checkpoint resume with your application before upgrading.

```python
from langgraph_checkpoint_cosmosdb import CosmosDBSaver

# COSMOSDB_ENDPOINT and optional COSMOSDB_KEY were configured before startup.
checkpointer = CosmosDBSaver(database_name="langgraph", container_name="checkpoints")
graph = builder.compile(checkpointer=checkpointer)
```

Keep a long-lived client for the worker lifetime, not one per request. The
caller owns shutdown: after all graph work finishes, call
`checkpointer.client.close()` in its lifecycle cleanup. In a bounded script,
use `try/finally` around graph use. Do **not** use
`close_cosmos_checkpointer` for directly constructed savers: it rejects savers
not tracked by the helper. The internally created identity credential has no
separate saver-managed cleanup contract; custom credential ownership needs an
upstream API rather than an assumed injection hook.

Microsoft's [CosmosClient API](https://learn.microsoft.com/en-us/python/api/azure-cosmos/azure.cosmos.cosmosclient?view=azure-python)
supports explicit key or `TokenCredential` authentication, but that SDK API is
**not** exposed by these saver versions. Multi-account credentials in one
process, dynamic credential substitution, a custom identity credential, or
caller-injected clients therefore require process separation or a future
upstream API. Do not patch saver internals or fork the provider under this issue.

For identity access, Azure resource `Owner`/`Contributor` alone is insufficient:
use [Cosmos data-plane RBAC](https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-connect-role-based-access-control)
(for example Built-in Data Contributor, appropriately scoped).
[`DefaultAzureCredential`](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential?view=azure-python)
chooses a chain, not necessarily Managed Identity; verify the deployed principal.
Keep TLS verification enabled. The helper's emulator-only `COSMOS_DISABLE_SSL`
is not an upstream saver setting; direct callers must use a trusted certificate
as described in [emulator guidance](https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-develop-emulator).

## Separate deprecations and release prerequisites

| Surface | Actual behavior | Replacement | Retirement status and prerequisite |
| --- | --- | --- | --- |
| `credential=` string alias | `DeprecationWarning`, `stacklevel=2`; successful use also emits the helper warning | Use `key=`; precedence remains key/alias, then `COSMOS_KEY`; providing both is an error | **Proposal: no retirement version now.** Maintainer must approve an alias-specific announcement/removal release and compatibility classification |
| `create_cosmos_checkpointer` as a whole | `DeprecationWarning`, `stacklevel=2` after key resolution; `key=` and env fallback still warn | Optional startup-managed direct saver above, only for suitable callers | **Proposal: no retirement version now.** First establish a supported replacement for affected callers, cleanup, key/identity paths and migration evidence; then maintainer approves separate releases |
| `close_cosmos_checkpointer` | No deprecation warning; helper-owned savers only; repeated close is a no-op | Retain for current helper users; direct users own client close | No retirement announced; assess lifecycle compatibility with any future helper removal |

Neither elapsed minor versions nor a docstring proves a warning period or a
working replacement. A future injection proposal must verify independent
instances, no environment writes, key and identity support, failure cleanup
and ownership at the proposed floor. The rich OpenAPI metadata bridge remains
outside this record: [#1035](https://github.com/yeongseon/azure-functions-langgraph-python/issues/1035)
has a separate unresolved support decision and is not evidence of Cosmos
deprecation timing.

## Synthetic validation and redaction boundary

`tests/test_cosmos_migration.py` exercises the existing helper's key, alias and
environment-fallback lifecycle with synthetic credentials and a fake saver;
checks warning count/category, value-free warnings/logs, restoration and silent
idempotent cleanup; and distinguishes direct-saver cleanup ownership. Existing
tests also demonstrate that unrelated threads can observe the temporary key:
a helper-local lock does **not** give process-wide isolation.

Redaction is limited: the helper's warnings and TLS warning do not include
credential values. Upstream SDK construction errors can retain sensitive text
in the original exception or its `__cause__`; the helper propagates that error.
A synthetic exception-chain test records this limitation rather than claiming
tracebacks are sanitized. Do not log raw backend exceptions, environment dumps,
keys or request credentials; sanitize at the application's error/log boundary.
No live authentication, persistence or cross-instance certification is claimed.
