"""Memory index factory tests (main.py wiring).

build_mnemis_index selects the enhanced-mode index: the local LLM-backed
NvidiaMemoryIndex when BRIDGESAT_LLM_API_KEY is set, otherwise the default
(unavailable-transport) adapter so enhanced mode degrades to SQLite exactly
as it did before the LLM layer existed.
"""

from __future__ import annotations

from pathlib import Path

from app.memory import build_mnemis_index
from app.memory.nvidia_backend import NvidiaMemoryIndex


def test_without_llm_key_returns_default_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BRIDGESAT_LLM_API_KEY", raising=False)
    adapter = build_mnemis_index(tmp_path / "mem.db")
    from app.memory.mnemis_backend import MnemisMemoryAdapter

    assert isinstance(adapter, MnemisMemoryAdapter)
    assert adapter.api_key == ""
    assert adapter._transport.__name__ == "_unconfigured_transport"


def test_with_llm_key_returns_nvidia_index_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGESAT_LLM_API_KEY", "nvapi-test-key")
    adapter = build_mnemis_index(tmp_path / "mem.db")
    from app.memory.mnemis_backend import MnemisMemoryAdapter

    assert isinstance(adapter, MnemisMemoryAdapter)
    assert isinstance(adapter._transport, NvidiaMemoryIndex)
    assert adapter.base_url == "http://local/nvidia-index"
