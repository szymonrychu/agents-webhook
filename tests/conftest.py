import pytest


@pytest.fixture
def db(migrated_db) -> str:
    """URL of a fresh database with the agents_webhook migrations applied."""
    return migrated_db("agents_webhook")
