import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads((FIXTURES / "availability_sample.json").read_text())
