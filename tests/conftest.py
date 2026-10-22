import os
from pathlib import Path

os.environ["AEAI_DOTENV"] = "0"          # a developer's .env (e.g. LLM_BACKEND=ollama) must not change test results

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.container import Container
from app.config import get_settings

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


@pytest.fixture(params=["numpy", "chroma"])
def container(tmp_path, monkeypatch, request):
    if request.param == "chroma":
        pytest.importorskip("chromadb")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("VECTOR_BACKEND", request.param)
    return Container(get_settings())


@pytest.fixture
def client(container):
    return TestClient(create_app(container))


@pytest.fixture
def sample():
    return lambda name: (SAMPLES / name).read_bytes()
