"""Adapters bridging MMFDB to external systems and host applications.

Each adapter maps MMFDB's provenance/metadata model to (or from) another system:
``chinet`` bridges ChiSurf/chinet fit sessions into MMFDB artifacts and
``elabftw`` provides a dependency-free eLabFTW REST v2 boundary.

Adapters may depend on their target system, but must keep those imports lazy
(function-local) so the core package still imports with only ``src`` on the path.
"""
