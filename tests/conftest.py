import pytest
import support


@pytest.fixture(scope="session")
def pack():
    """The pack imported ComfyUI-style (root __init__.py, no ComfyUI needed)."""
    return support.load_pack()
