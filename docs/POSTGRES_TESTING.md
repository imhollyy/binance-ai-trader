# BT-002D · Database and Audit Verification

Automated CI now runs PostgreSQL 16 as a test-only GitHub Actions service.

Checks:
- Apply the Alembic upgrade twice to verify repeatability.
- Write settings through the guarded API and verify both settings and audit metadata.
- Dispose the SQLAlchemy engine and start a fresh FastAPI app with a new engine.
- Verify settings persist and stale update requests return HTTP 409 without writing extra audit events.
- Confirm rejected `live` mode attempts return HTTP 422 without changing the DB.
- Verify the audit endpoint requires the development admin token and returns metadata only.

These are CI integration checks, **not** a physical Docker Compose volume-recovery drill or a production backup/restore test. Those remain manual acceptance criteria for BT-002D.

No exchange credentials, real orders, Futures execution or live worker were added.
