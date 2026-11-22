import os
import tempfile
from pathlib import Path

os.environ.setdefault('DATABASE_URL', 'sqlite+pysqlite:////tmp/unused.db')
os.environ.setdefault('REDIS_URL', 'redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('SEED_DEMO', 'false')

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import database
from src.config.settings import ARTIFACTS  # noqa: F401
from src.models.identity import User
from src.utils.security import hash_password


@pytest.fixture(scope='session')
def tmp_artifact_dir():
    with tempfile.TemporaryDirectory() as folder:
        from src.config import settings
        settings.ARTIFACTS = Path(folder)
        from src.utils import exports
        exports.ARTIFACTS = Path(folder)
        from src.routes import distribution
        distribution.ARTIFACTS = Path(folder)
        yield Path(folder)


@pytest.fixture()
def db_session(tmp_artifact_dir):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(engine, expire_on_commit=False)
    database.engine = engine
    database.Session = testing_session
    from src.services import execution
    execution.Session = testing_session
    from src import main
    main.engine = engine
    from src.config.database import Base
    import src.models.identity  # noqa: F401
    import src.models.jobs  # noqa: F401
    import src.models.source  # noqa: F401
    Base.metadata.create_all(engine)
    with testing_session.begin() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(username=role, role=role,
                        password_hash=hash_password(f'{role}-pw')))
    yield testing_session
    engine.dispose()


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client
