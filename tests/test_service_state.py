"""T3: service-state record carries runtime (never config) state.

Suite convention (see test_domain.py): validation and defaults only, no
database mocking. The SCHEMALESS `open_notebook` table needs no migration
for this record (cf. migration 23).
"""

from __future__ import annotations

from open_notebook.domain.service_state import ServiceState


def test_record_id_targets_open_notebook_table():
    assert ServiceState.record_id == "open_notebook:service_state"


def test_draining_defaults_false():
    ServiceState.clear_instance()
    try:
        state = ServiceState()
        assert state.draining is False
    finally:
        ServiceState.clear_instance()


def test_singleton_per_record_id():
    ServiceState.clear_instance()
    try:
        first = ServiceState()
        second = ServiceState(draining=True)
        assert first is second
        assert second.draining is True
    finally:
        ServiceState.clear_instance()
