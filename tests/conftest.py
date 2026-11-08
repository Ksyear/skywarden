import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def synthetic_room(tmp_path_factory):
    from sim.synthetic_scene import generate
    return generate(tmp_path_factory.mktemp("synthetic") / "room", n_frames=12, size=(400, 300), seed=3)
