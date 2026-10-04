"""MedForge Database Package."""

from core.database.schema import (
    V3_SCHEMA_DDL,
    LEGACY_SCHEMA_DDL,
    NODE_TYPES,
    PREREQUISITE_TYPES,
    WEAKNESS_SEVERITY,
    SESSION_TYPES,
    SPACED_REPETITION_STATES,
    SPACED_REPETITION_ITEM_TYPES,
    MEDICAL_PUBLICATION_TYPES,
)
from core.database.migrate_v3 import (
    migrate_database,
    create_snapshot_backup,
    is_v3_migrated,
    rollback_migration,
)

__all__ = [
    "V3_SCHEMA_DDL",
    "LEGACY_SCHEMA_DDL",
    "NODE_TYPES",
    "PREREQUISITE_TYPES",
    "WEAKNESS_SEVERITY",
    "SESSION_TYPES",
    "SPACED_REPETITION_STATES",
    "SPACED_REPETITION_ITEM_TYPES",
    "MEDICAL_PUBLICATION_TYPES",
    "migrate_database",
    "create_snapshot_backup",
    "is_v3_migrated",
    "rollback_migration",
]
