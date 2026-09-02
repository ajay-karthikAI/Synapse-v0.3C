"""
synapse.cli
===========
Command-line entry points.

Currently one command, :mod:`synapse.cli.migrate_artifacts`, which converts
legacy pickle corpora to the JSONL + manifest format. It is a one-way,
non-destructive migration: it reads the pickle, writes new files elsewhere, and
never modifies or deletes the input.
"""

from __future__ import annotations  # Postponed annotations

__all__: list[str] = []
