import json, os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("UNSUBSCRIBE_SECRET", "test-secret")
for k in ("BRIEF_LLM_MODEL", "OPENAI_API_KEY", "AIRROI_API_KEY"):
    os.environ.pop(k, None)

URL = "https://www.airbnb.com/rooms/50439899?source_impression_id=abc"
os.environ.setdefault("TEST_DATABASE_URL", "")
if not os.environ["TEST_DATABASE_URL"]:
    os.environ.pop("TEST_DATABASE_URL")

@pytest.fixture
def pdp():
    return json.loads((Path(__file__).parent / "fixtures" / "hermosa_pdp.json").read_text())

@pytest.fixture
def listing(pdp):
    from brief.listing import parse_listing
    return parse_listing(pdp, URL)
