# Docker Compose persistence verification

GitHub Actions runs a **disposable, test-only** PostgreSQL 16 + API stack via Docker Compose, writes paper settings and a harmless audit entry, executes `docker compose down`, rebuilds/recreates the containers, then verifies the same settings and audit versions remain in the named database volume.

No exchange credentials, live orders, futures execution or production account access are involved. The test uses hardcoded CI fixture credentials against `127.0.0.1` only; these are **not** Binance secrets.

This proves the basic down/up **volume retention** behavior in a clean runner. It does **not** prove backup/restore, disk-failure recovery, or server recovery after a destroyed volume. Never run `docker compose down -v` on actual data unless you intend to delete it.
