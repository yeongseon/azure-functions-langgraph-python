# Security Policy

## Reporting a Vulnerability

We take security seriously. If you discover a vulnerability, please report it
responsibly and avoid public disclosure until a fix is available.

### Preferred: GitHub Security Advisory

1. Go to the [Security Advisories page](https://github.com/yeongseon/azure-functions-langgraph-python/security/advisories/new)
2. Click "Report a vulnerability"
3. Submit the details in the private advisory

### Alternative: Email

If you prefer email, contact: yeongseon.choe@gmail.com

### What to Include

* A clear description of the issue
* Steps to reproduce
* Impact assessment
* Suggested mitigation or fix, if available

### Response Timeline

* Initial response: within 48 hours
* Status update: within 7 days

## Supported Versions

| Version | Supported |
| ------- | --------- |
| Latest release | :white_check_mark: |
| Older releases | :x: |

## Production boundaries

`LangGraphApp` defaults to FUNCTION authentication. A host/function key grants
application access, **not user/tenant ownership**. ANONYMOUS examples are local
demonstrations, not production authorization templates (the minimal anonymous
liveness probe is a deliberate exception). Applications own JWT/Easy Auth
principal verification, graph entitlement, thread ownership, revocation and
shared-store isolation. See [the route threat model](docs/security.md) before
deployment: native RUN callbacks are an explicit gap tracked by #687; true
streaming, assistant inventory and Durable management require external policy.

## Gates and evidence

| Where | Check | Policy |
| --- | --- | --- |
| PR, main, weekly `security.yml` | Existing source Bandit plus explicit `src/`, `examples/`, `scripts/`, `tools/`, `tests/host_smoke` inventory | Blocking, including missing/zero scan scope and unreviewed findings |
| PR, main, weekly `security.yml` | Pinned Semgrep `p/python` action | Existing blocking action preserved; additional action-only summary is reporting-only, analyzed file/finding counts unknown (`INCOMPLETE`) |
| Existing CodeQL workflow | Python/code scanning | Existing configuration unchanged |
| Scheduled `dependency-audit.yml` | Development dependency audit | Advisory; not a substitute for candidate auditing |
| Tag/recovery `publish-pypi.yml`, `runtime-audit` | Exact built wheel, base plus each runtime extra | Blocking all unexcepted advisories, including unknown severity |
| Existing SBOM/release workflows | SPDX, exact artifact reuse, Azure certification, OIDC | Unchanged; not replaced by static scans |

Runtime profiles: base, azure-blob, azure-table, azure-identity, postgres, sqlite,
cosmos, otel, streaming, durable, openapi. Each resolves separately from the same
candidate wheel on Python 3.11. Scanner dependencies are installed outside the
temporary target; only the candidate's resolved runtime dependencies are audited
with `pip-audit==2.10.1`. `dev` and `docs` are not production profiles. This is
not certification of every extras combination, oldest dependency constraint,
Python 3.12–3.14 environment, or deployment's own dependencies. Resolution errors
block that profile as `TOOL_ERROR`; optional extras are never silently skipped.
The wheel METADATA name/version must exactly match the installed candidate before
exclusion. A pip-free target venv is installed through external `pip --python`;
only that verified candidate is excluded from the frozen third-party closure.
Absent/mismatched identity, empty inventories and skipped third-party packages
remain blocking. Unpublished first-party versions do not require a PyPI lookup.

## Automated security controls

Use Makefile entry points locally and in CI:

- `make security` includes the expanded executable-surface inventory scan.
- `make security-inventory` / `make security-bandit-evidence` write Bandit evidence.
- `make security-semgrep-evidence SCAN_OUTCOME=success` records **action outcome only**
  as INCOMPLETE and tolerates exactly exit 3; the pinned action remains the gate.
  An action failure remains nonzero and is never converted to success.
- `make security-audit-policy` validates expiring runtime exceptions.
- `make security-runtime-audit WHEEL=/path/to/candidate.whl PROFILE=base` blocks
  unsafe or incomplete resolved candidate evidence (repeat for each listed profile).
- `make security-runtime-report REPORT=/path/to/audit.json AUDIT_EXIT=0` classifies
  saved evidence without rerunning installation or scanning; non-PASS still exits nonzero.

Set `SECURITY_PYTHON=python` in selected-interpreter CI; local targets default to
the existing Hatch interpreter. Collectors and artifact uploads use `if: always()`.

Evidence statuses and helper exits: `PASS`/0 (usable nonempty evidence, no blocking
findings), `FAIL`/1 (unexcepted findings), `TOOL_ERROR`/2 (scanner/installation,
policy or malformed-output failure), `INCOMPLETE`/3 (zero/missing scope or skipped
packages). Missing JSON never means "no vulnerabilities". Runtime pip-audit does
not reliably supply severity, so **every advisory blocks**, a strict superset of
a high/critical-only gate. No severity lookup failure can downgrade an advisory.

`security-results/**/summary.json` and Job Summary contain allow-listed status,
counts, tool versions, rule/advisory identifiers, package names, relative static
paths/lines and candidate SHA256, not raw scanner output. Source excerpts,
exception/diagnostic prose, user input, headers, credentials and connection
strings are discarded. Available redacted evidence uploads with `if: always()`.
This guarantee applies to these generated summaries, **not** the existing
scanner action console, application operator logs, or arbitrary test output.
No SARIF/JUnit is invented when a scanner provides no such evidence contract.

## Exceptions

Keep the existing mandatory `.github/pip-audit-ignore.txt` parser; do not replace
it with a separate severity policy. Entries use
`ID # reason # owner NAME # expires YYYY-MM-DD`. Use CVE/GHSA IDs, a substantive
reason and accountable owner, with future expiry at most **90 days** away.
Expired, malformed, duplicate, ownerless or overlong entries fail validation.
The older parser enforced expiry but had no maximum or owner requirement; this
policy adds both. Renewals require a fresh review, not an automatic date bump.

Static exceptions in `security/bandit-policy.json` additionally pin exact
file/line/rule, added date and reviewed-static classification. Existing inline
`nosec` comments are inventoried exactly and scanned with `--ignore-nosec`;
runtime source is not changed. Blanket/new/unlisted suppressions fail closed.
Reviewed subprocess findings rely on controlled developer/CI PATH and explicit
argv, not shell execution. The local SQLite `/tmp` sample is a code/config review
finding until runtime impact is independently demonstrated: use a private path
on shared machines; its local-only exception is not a production endorsement.

## Local emulators

Never expose emulator ports, keys or certificates in production. Loopback
bindings, port overrides and unique run-owned Compose cleanup are **introduced by
the verification-contract change (#686, PR #706)**; they are not yet on this
branch's base. See [emulator guidance](docs/security.md#local-emulators-and-resource-ownership)
for the interim override requirement. Trust only the local Cosmos emulator
certificate in a run-scoped CA bundle; never disable production TLS validation.
