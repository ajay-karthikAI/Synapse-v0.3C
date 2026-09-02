"""
synapse._legacy
===============
Quarantine for pre-migration artifact formats.

Everything in this package exists solely to read data written by the previous
implementation, once, so it can be converted to a safe format. The underscore
prefix marks it private, and an import-graph test asserts that nothing outside
``synapse.cli.migrate_artifacts`` and the test suite imports from it.

Runtime application paths must never reach this package. Once migration is
complete for a deployment, this package is deleted.
"""

from __future__ import annotations  # Postponed annotations

__all__: list[
    str
] = []  # Nothing is re-exported: importers must reach for the specific module, making every use visible in a grep
