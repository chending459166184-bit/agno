#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.db import Database


def main() -> None:
    settings = get_settings()
    database = Database(settings.resolved_db_file)
    database.init_schema()
    database.seed_demo_data(
        settings.project_root,
        settings.resolved_workspace_root,
        settings.resolved_external_agent_catalog_file,
    )
    print(f"Seed completed: {settings.resolved_db_file}")
    print(f"Workspace root: {settings.resolved_workspace_root}")


if __name__ == "__main__":
    main()
