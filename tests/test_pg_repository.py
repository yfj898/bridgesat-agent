from __future__ import annotations

import pytest
from app.infrastructure import pg
from app.infrastructure.migration_runner import migrate_database
from app.models import Skill, StudentCreate
from app.repository import StudentRepository

TENANT = "tenant_test"


@pytest.fixture()
def repo():
    admin = pg.connect_admin()
    migrate_database(admin)
    admin.close()
    conn = pg.connect()
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
    conn.commit()
    yield StudentRepository(conn)
    conn.close()
    admin = pg.connect_admin()
    admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    admin.commit()
    admin.close()


def test_create_and_get(repo):
    student = repo.create(StudentCreate(name="Ada", daily_minutes=30, target_score=600))
    fetched = repo.get(student.id)
    assert fetched is not None
    assert fetched.name == "Ada"
    assert fetched.mastery[Skill("linear_equations")] == 0.5


def test_get_other_tenant_returns_none(repo):
    student = repo.create(StudentCreate(name="Ada", daily_minutes=30, target_score=600))
    repo2_conn = pg.connect()
    repo2_conn.execute("SELECT set_config('app.tenant_id', 'tenant_other', false)")
    repo2_conn.commit()
    repo2 = StudentRepository(repo2_conn)
    assert repo2.get(student.id) is None
    repo2_conn.close()


def test_update_mastery(repo):
    student = repo.create(StudentCreate(name="Ada", daily_minutes=30, target_score=600))
    repo.update_mastery(student.id, {Skill("linear_equations"): 0.9})
    fetched = repo.get(student.id)
    assert fetched.mastery[Skill("linear_equations")] == 0.9
