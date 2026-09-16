import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    """Run @pytest.mark.anyio tests on asyncio only (no trio dependency)."""
    return "asyncio"
