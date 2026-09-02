"""
Test setup.

Every test runs against a throwaway data directory so the suite can never
touch the real local_data folder. The environment variable is set before any
application module is imported, because config.py reads it at import time.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TEST_DATA = Path(tempfile.mkdtemp(prefix="private-ai-tests-"))
os.environ["PRIVATE_AI_DATA_DIR"] = str(_TEST_DATA)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import config, db, vectorstore  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh database for every test."""
    config.ensure_dirs()
    db.init_db()
    db.delete_all_data()
    vectorstore.invalidate()
    yield
    db.delete_all_data()
    vectorstore.invalidate()


@pytest.fixture
def tmp_docs(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    return d


class FakeEmbedder:
    """
    Deterministic offline embedder.

    Maps text to a bag-of-words vector over a fixed vocabulary so that
    similarity behaves sensibly without needing the Ollama runtime.
    """

    VOCAB = [
        "revenue", "risk", "supplier", "osaka", "billing", "atlas", "timeline",
        "beta", "march", "june", "growth", "quarter", "budget", "invoice",
        "customer", "region", "north", "south", "amount", "total", "report",
        "concentration", "engine", "codename", "ships", "expect", "steady",
    ]

    def __call__(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            low = t.lower()
            vec = [float(low.count(w)) for w in self.VOCAB]
            if not any(vec):
                vec[0] = 0.001
            out.append(vec)
        return out


@pytest.fixture
def fake_embed() -> FakeEmbedder:
    return FakeEmbedder()
