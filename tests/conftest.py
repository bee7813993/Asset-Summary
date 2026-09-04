from __future__ import annotations

from pathlib import Path

import pytest

from asset_summary.core.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")
