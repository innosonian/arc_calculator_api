"""Read one bounded page of operational logs from an existing local installation.

The server must already own/start its DB. This tool neither initializes nor
changes a table, installation, credential, file, or AWS resource.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var/local-server")
    parser.add_argument("--db-port", type=int, default=8001)
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"), help="UTC YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=100, help="1..500 records per page")
    parser.add_argument("--after", help="next_cursor from the preceding page on the same date")
    args = parser.parse_args(argv)
    database = None
    try:
        if not 1 <= args.db_port <= 65535:
            raise ValueError("Invalid local port.")
        from local_server.cli import isolated_environment, restrict_outbound
        isolated_environment()
        restrict_outbound(args.db_port, "127.0.0.1", args.db_port)
        from local_server.database import prepare_material, connect_application
        from mock_journey.log_storage import DynamoLogStore
        material = prepare_material(args.data_dir.absolute(), initialize=False)
        database = connect_application(f"http://127.0.0.1:{args.db_port}", material, initialize=False)
        store = DynamoLogStore(database.client, database.table_name, material.environment, owns_client=False)
        page = store.read_page(args.date, limit=args.limit, after=args.after)
        print(json.dumps(page, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        print("Local operational logs could not be read. Check the existing installation, DB port, date and cursor.", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
