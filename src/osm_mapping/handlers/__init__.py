"""osm-mapping domain handlers — registration entry points."""

from .osm_mapping_handlers import register_handlers, register_poller

__all__ = ["register_all_registry_handlers", "register_all_handlers"]


def register_all_registry_handlers(runner) -> None:
    register_handlers(runner)


def register_all_handlers(poller) -> None:
    register_poller(poller)
