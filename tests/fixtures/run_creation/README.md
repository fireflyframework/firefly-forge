The `schema26_migrations.py` fixture is the exact `src/firefly_forge/migrations.py`
source at commit `dfd9037` (the revision 26 parent of run-creation requests).
SHA-256: `138e56f45144ad1d25ab1eb5364dcd8bb19b6c9329ae5695d77bafa1f42d2326`.

The upgrade test creates revision 26 with this previous migrator before populating
native execution, write-ahead intent, campaign report and delivery-plan receipts.
It then invokes revision 27 and compares the stored prior rows exactly. It does
not relabel or delete a current schema's migration records to simulate 26.
Existing entity definitions used by the old migrator are unchanged in revision 27.

These tests are prepared for the sole PostgreSQL/API operator. They have not been
executed by the authoring lane. Each native fixture requires the existing isolated
`FORGE_TEST_DATABASE_URL` ending in `_test` and owns its disposable schema; it does
not acquire models, images, cloud resources, or external identity services.

For the additive revision28 gate, `schema27_migrations.py` and
`schema27_requests.py` are byte-exact source fixtures from
`917f1bff8c5535d5c8d98af68809e3e18b57c6f8`. The upgrade test creates a real
revision27 schema, admits an original receipt with that previous service, and
only then applies revision28. Neither fixture imports the new control protocol.
Their SHA-256 values are pinned in the test. The archived revision26 migrator
remains byte-identical. The current revision26-to-latest test now expects28;
the frozen e436 revision26-to27 qualification source remains unchanged.
