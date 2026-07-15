"""Deprecated adapter implementations kept only for regression coverage.

Production code must import :mod:`adapters.stagehand_adapter` or the adapter
registry.  This package intentionally does not import the historical
monolith, so normal Jobops startup never pays its import or maintenance cost.
"""
