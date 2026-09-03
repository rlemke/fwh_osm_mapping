"""Backend-aware paths for the osm_mapping domain.

⚠️ This is now a THIN SHIM over ``facetwork.domains.storage``. 21 of the 29
fwh_* repos shipped their own copy of this module doing the same job, and the
copies had drifted — a bug fixed in one stayed unfixed in twenty. The shared
layer owns the behaviour; this file exists only to keep the import path and the
public names this package already uses.

⚠️ The layout arguments are NOT cosmetic: they name where this domain's data
already sits in the object store. Changing them orphans that cache rather than
moving it, so they pin the existing layout exactly.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import IO

from facetwork.domains.storage import domain_storage, is_remote, join  # noqa: F401

_S = domain_storage("osm_mapping", path_name="osm-mapping")


def cache_root() -> str:
    return _S.cache_root()


def output_root() -> str:
    return _S.output_root()


def exists(path: str) -> bool:
    return _S.exists(path)


def localize(path: str) -> str:
    return _S.localize(path)


def open_read(path: str, mode: str = "r", **kw) -> IO:
    return _S.open_read(path, mode, **kw)


def open_write(path: str, mode: str = "w", **kw) -> Iterator[IO]:
    return _S.open_write(path, mode, **kw)
