import os
from pathlib import Path

import pytest

from app import question_bank

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PACKS_ROOT = FIXTURES_DIR / "packs"


@pytest.fixture(autouse=True)
def use_fixture_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGESAT_PACKS_ROOT", str(PACKS_ROOT))
    question_bank.clear_cache()
    yield
    question_bank.clear_cache()
