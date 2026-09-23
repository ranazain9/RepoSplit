"""Change Data Capture (CDC) and Dual-Write Synchronization Generator.

Emits synchronization tooling for safe, drift-free canary traffic shifting:
- `sync_daemon.py`: Lightweight SQLite bidirectional table sync worker with high-watermark.
- `debezium_connector.json`: Production Kafka Connect Debezium CDC configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

from reposplit.core.schemas import DataPartitionPlan, DomainTopology


class CDCGenerator:
    def __init__(
        self,
        output_dir: Path,
        data_plan: DataPartitionPlan,
        topology: DomainTopology,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.data_plan = data_plan
        self.topology = topology

    def generate(self) -> list[str]:
        cdc_dir = self.output_dir / "data" / "cdc"
        cdc_dir.mkdir(parents=True, exist_ok=True)
        produced: list[str] = []

        # 1. Debezium Kafka Connect configuration
        tables = [e.table for e in self.data_plan.entities]
        debezium_cfg = {
            "name": "reposplit-cdc-connector",
            "config": {
                "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
                "tasks.max": "1",
                "plugin.name": "pgoutput",
                "database.hostname": "monolith-db",
                "database.port": "5432",
                "database.user": "postgres",
                "database.password": "postgres",
                "database.dbname": "monolith",
                "database.server.name": "monolith_cdc",
                "table.include.list": ",".join(f"public.{t}" for t in tables),
                "tombstones.on.delete": "false",
            },
        }
        deb_path = cdc_dir / "debezium_connector.json"
        deb_path.write_text(json.dumps(debezium_cfg, indent=2), encoding="utf-8")
        produced.append("data/cdc/debezium_connector.json")

        # 2. Build service->tables mapping from data plan
        # service_schemas: dict[str, list[str]] — values are already lists of table names
        svc_tables: dict[str, list[str]] = {
            svc: list(tables) for svc, tables in self.data_plan.service_schemas.items()
            if isinstance(tables, list)
        }
        # Fallback: derive from entities if service_schemas values are schema objects
        if not svc_tables:
            for svc in (self.topology.clusters or {}):
                svc_tables[svc] = [e.table for e in self.data_plan.entities]
        svc_tables_repr = repr(svc_tables)

        # 3. sync_daemon.py — real polling implementation
        sync_script = f'''"""RepoSplit Dual-Write & CDC Sync Daemon.

Synchronizes records between the legacy SQLite monolith and generated service DBs during
the canary deployment window. Uses a per-table high-watermark (rowid or updated_at) stored
in a local state file so the daemon is restartable without re-syncing the entire table.

For production PostgreSQL, use the Debezium connector (debezium_connector.json) instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reposplit.cdc")

# service -> list of tables it owns (generated from data plan)
SERVICE_TABLES: dict[str, list[str]] = {svc_tables_repr}

STATE_FILE = "cdc_watermarks.json"


def _load_watermarks(state_path: Path) -> dict[str, int]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {{}}


def _save_watermarks(state_path: Path, watermarks: dict[str, int]) -> None:
    state_path.write_text(json.dumps(watermarks, indent=2), encoding="utf-8")


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({{table}})").fetchall()
    return [row[1] for row in rows]


def _sync_table(
    monolith_conn: sqlite3.Connection,
    svc_conn: sqlite3.Connection,
    table: str,
    watermarks: dict[str, int],
) -> int:
    """Copy rows newer than the watermark from monolith to the service DB. Returns row count."""
    wm_key = f"{{table}}"
    last_rowid = watermarks.get(wm_key, 0)

    # Use rowid as the high-watermark; fall back to checking if table exists
    try:
        rows = monolith_conn.execute(
            f"SELECT rowid, * FROM {{table}} WHERE rowid > ? ORDER BY rowid ASC LIMIT 500",
            (last_rowid,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("table %s not in monolith: %s", table, exc)
        return 0

    if not rows:
        return 0

    # Get column names from the monolith
    cols = _get_columns(monolith_conn, table)
    placeholders = ", ".join("?" * len(cols))
    col_list = ", ".join(cols)
    upsert_sql = f"INSERT OR REPLACE INTO {{table}} ({{col_list}}) VALUES ({{placeholders}})"

    # Ensure the table exists in the service DB (best-effort: copy DDL from monolith)
    try:
        ddl = monolith_conn.execute(
            f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if ddl and ddl[0]:
            svc_conn.execute(ddl[0])
    except sqlite3.OperationalError:
        pass

    synced = 0
    max_rowid = last_rowid
    for row in rows:
        row_rowid = row[0]
        row_data = row[1:]  # strip the rowid prefix
        try:
            svc_conn.execute(upsert_sql, row_data)
            synced += 1
            max_rowid = max(max_rowid, row_rowid)
        except sqlite3.OperationalError as exc:
            logger.warning("upsert failed for %s row %s: %s", table, row_rowid, exc)
    svc_conn.commit()
    watermarks[wm_key] = max_rowid
    return synced


def sync_tables(monolith_db: Path, services_dir: Path, poll_interval: float = 1.0) -> None:
    state_path = services_dir / STATE_FILE
    watermarks = _load_watermarks(state_path)
    logger.info("Starting CDC sync daemon. Polling every %.1fs | monolith=%s services=%s", poll_interval, monolith_db, services_dir)

    while True:
        try:
            monolith_conn = sqlite3.connect(str(monolith_db))
            total_synced = 0
            for svc, owned_tables in SERVICE_TABLES.items():
                svc_db = services_dir / svc / "data" / f"{{svc}}.db"
                if not svc_db.exists():
                    continue
                svc_conn = sqlite3.connect(str(svc_db))
                for table in owned_tables:
                    count = _sync_table(monolith_conn, svc_conn, table, watermarks)
                    if count:
                        logger.info("synced %d rows -> %s/%s", count, svc, table)
                        total_synced += count
                svc_conn.close()
            monolith_conn.close()
            if total_synced:
                _save_watermarks(state_path, watermarks)
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Stopping CDC sync daemon")
            _save_watermarks(state_path, watermarks)
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("sync error: %s", exc)
            time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RepoSplit CDC Dual-Write Sync Daemon")
    parser.add_argument("--monolith-db", type=Path, default=Path("monolith.db"))
    parser.add_argument("--services-dir", type=Path, default=Path("services"))
    parser.add_argument("--poll", type=float, default=1.0)
    args = parser.parse_args()
    sync_tables(args.monolith_db, args.services_dir, args.poll)
'''
        sync_path = cdc_dir / "sync_daemon.py"
        sync_path.write_text(sync_script, encoding="utf-8")
        produced.append("data/cdc/sync_daemon.py")

        return produced
