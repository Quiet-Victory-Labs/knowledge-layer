"""Recovery must compare every persisted registry table at the current head."""

import importlib.util
from pathlib import Path

from knowledge_layer.store import KnowledgeStore


def test_recovery_covers_current_persisted_schema():
    path = Path(__file__).resolve().parents[1] / "scripts" / "drill_postgres_recovery.py"
    spec = importlib.util.spec_from_file_location("recovery_drill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = KnowledgeStore(database_url="sqlite:///:memory:", initialize_schema=False)
    try:
        assert set(module.TABLES_BY_REVISION["0016_builds"]) == set(store.metadata.tables)
    finally:
        store.close()
