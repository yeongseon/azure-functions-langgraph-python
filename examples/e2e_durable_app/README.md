# e2e_durable_app

Test fixture for the **optional** real-Azure Durable lane (#811). It is not a tutorial; see
`examples/durable_async_agent` for that. Deployed only by `.github/workflows/e2e-azure-durable.yml`
(dispatch only, never a release gate) from a **built wheel** of the ref under test.

Status: the lane has run on real Azure, but interrupted-Activity recovery remains unproven.
See the [dated observations and limits](../../docs/production-guide.md#live-durable-observations-2026-11-24-y1-python-312),
including the failed strengthened-probe run. Nothing here is certification or a claim of
exactly-once execution, automatic Activity abort or universal checkpoint fencing.

Tested combination when dispatched: Linux Consumption (Y1), Python 3.12, `azure-functions-durable`
1.x, Azure Storage task-hub backend, one app instance. Other plans, SDK lines, Python versions,
cross-instance topologies and storage backends are **uncertified**.

The tests record, and do not assume, how many times the single Activity ran after a host restart
and whether an Activity finishes after a terminate. Evidence is uploaded as the
`azure-durable-evidence` artifact (statuses, counts and salted process tags). The two modes are
restart and stop / bounded poll for `Stopped` / start on the dispatch's own app. Each uses a
240 s hold and a 1,200 s terminal bound; failed or missing observations remain failures.
