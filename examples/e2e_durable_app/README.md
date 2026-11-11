# e2e_durable_app

Test fixture for the **optional** real-Azure Durable lane (#811). It is not a tutorial; see
`examples/durable_async_agent` for that. Deployed only by `.github/workflows/e2e-azure-durable.yml`
(dispatch only, never a release gate) from a **built wheel** of the ref under test.

Status: the lane has **not yet been run on real Azure**. Until a run is recorded, nothing here is
a certification, and no claim of exactly-once execution, automatic Activity abort or universal
checkpoint fencing should be drawn from it.

Tested combination when dispatched: Linux Consumption (Y1), Python 3.12, `azure-functions-durable`
1.x, Azure Storage task-hub backend, one app instance. Other plans, SDK lines, Python versions,
cross-instance topologies and storage backends are **uncertified**.

The tests record, and do not assume, how many times the single Activity ran after a host restart
and whether an Activity finishes after a terminate. Evidence is uploaded as the
`azure-durable-evidence` artifact (statuses, counts and short instance tags only).
