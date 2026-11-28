# e2e_durable_app

Test fixture for the **optional** real-Azure Durable lane (#811). It is not a tutorial; see
`examples/durable_async_agent` for that. Deployed only by `.github/workflows/e2e-azure-durable.yml`
(dispatch only, never a release gate) from a **built wheel** of the ref under test.

Status: one corrected live run observed different-process recovery in both restart modes.
See the [dated observations and limits](../../docs/production-guide.md#live-durable-observations-2026-11-24-y1-python-312),
including the failed strengthened-probe run and its unresolved timeout cause. Nothing here is certification or a claim of
exactly-once execution, automatic Activity abort or universal checkpoint fencing.

Tested combination when dispatched: Linux Consumption (Y1), Python 3.12, `azure-functions-durable`
1.x, Azure Storage task-hub backend, one app instance. Other plans, SDK lines, Python versions,
cross-instance topologies and storage backends are **uncertified**.

The tests record, and do not assume, how many times the single Activity ran after a host restart
and whether an Activity finishes after a terminate. Evidence is uploaded as the
`azure-durable-evidence` artifact (statuses, counts and salted process tags). The two modes are
restart and stop / bounded poll for `Stopped` / start on the dispatch's own app. Each uses a
240 s hold and a 1,200 s terminal bound; failed or missing observations remain failures.

## Post-effect restart probe

The [2026-11-28 observation](../../docs/production-guide.md#post-effect-interruption-observation-2026-11-28-y1-python-312)
adds a separately opted-in scenario. It commits the effect blob, holds **inside
the effect node before returning its checkpoint result**, and restarts the host.
Redelivery calls the tool again with the same key; create-if-absent suppresses
the second effect and skips the hold. This atomic substitute is not an atomic
claim-plus-external-tool protocol. The normal dispatch workflow is unchanged.

To reproduce in an authorized **owned temporary** Y1 environment:

1. Build the candidate wheel from the exact source ref with `make build` (the
   recorded manual run used `uv build --wheel`). Copy this directory to a scratch
   deployment payload, put the wheel under `wheels/`, and append
   `./wheels/<wheel-filename>[durable]` to its `requirements.txt`. Record the source
   SHA and wheel SHA-256. Do not publish an editable install or substitute PyPI.
2. Create a unique tagged resource group and deploy `infra/main.bicep` with
   `functionAppName=<APP> storageName=<STORAGE> location=koreacentral
   enableAppInsights=false`. This creates only Y1, Standard_LRS and the existing
   Blob containers; it uses connection-string-based host storage. Do not enable
   Application Insights/default workspaces or reuse someone else's group.
3. Set **only on that app** `E2E_POST_EFFECT_HOLD_SECONDS=240` with
   `az functionapp config appsettings set -g <RG> -n <APP> --settings
   E2E_POST_EFFECT_HOLD_SECONDS=240 -o none`. Wait for provisioning, then from the
   payload run `func azure functionapp publish <APP> --python --build remote`.
4. From the repository root with pytest/requests available, run:

   ```bash
   E2E_DURABLE_BASE_URL=https://<APP>.azurewebsites.net \
   E2E_DURABLE_RESTART_CMD='az functionapp restart -g <RG> -n <APP> -o none' \
   E2E_DURABLE_POST_EFFECT_PROBE=1 \
   E2E_DURABLE_EVIDENCE=<scratch>/evidence.json \
     python -m pytest tests/azure_optional/test_post_effect_restart.py \
       -x -v -m e2e_durable -o addopts='' --tb=short \
       --junitxml=<scratch>/results.xml
   python tools/check_junit.py <scratch>/results.xml \
     --area 'post-effect recovery=test_post_effect_restart'
   ```

   The probe requires a fresh applied-effect observation with no completion,
   then permits 1,200 s to terminal. Same-process drain, missing observations,
   failed control, or lack of duplicate-call suppression do not pass it. It is
   skipped without explicit opt-in and is not a required CI/release gate.
5. Use a shell EXIT/signal trap or `finally` **before provisioning** to delete
   your resource group on success/failure. Delete synchronously immediately after
   evidence capture; verify `az group exists -n <RG>` is `false`, your experiment
   tag selects no resources, and no new `DefaultResourceGroup-*` exists. Keep
   secrets only in process environment; redact evidence. Write findings only
   after cleanup verification. No role assignment or Entra object is needed.

Do not combine this app setting with the normal six-scenario dispatch: its
240 s post-effect holds intentionally change that fixture's timings. Other
Azure plans and backend combinations are not covered by the recorded run.
