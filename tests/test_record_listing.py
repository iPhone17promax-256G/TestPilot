"""Real-MySQL regressions for listing records with large JSON payloads."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, insert, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from testpilot import storage
from testpilot.workspace import project_scope, user_scope


@pytest.fixture
def listing_store(monkeypatch):
    # These tests deliberately insert oversized records; never use application data.
    if storage.engine.dialect.name != "mysql":
        pytest.skip("The sort-memory regression requires real MySQL.")
    if not (storage.engine.url.database or "").startswith("testpilot_checks_"):
        pytest.fail("Run record-listing tests in the disposable database from scripts/check.py.")

    engine = create_engine(storage.engine.url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def small_sort_buffer(connection, _):
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION sort_buffer_size = 32768")

    monkeypatch.setattr(storage, "Session", sessionmaker(engine, expire_on_commit=False))
    identities = []

    def save(rows):
        identities.extend(row["id"] for row in rows)
        with engine.begin() as connection:
            connection.execute(insert(storage.Record), rows)

    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT @@SESSION.sort_buffer_size")) == 32768
        yield save, engine
    finally:
        try:
            with engine.begin() as connection:
                for offset in range(0, len(identities), 500):
                    connection.execute(delete(storage.Record).where(
                        storage.Record.id.in_(identities[offset:offset + 500])))
        finally:
            engine.dispose()


def record(identity, kind, created_at, payload, project=""):
    return dict(id=identity, kind=kind, project_id=project, status="",
                created_at=created_at, updated_at=created_at, payload=payload)


def test_large_json_listing_avoids_mysql_sort_memory_error(listing_store):
    save, engine = listing_store
    project, owner = "P-" + uuid4().hex, "U-" + uuid4().hex
    created = datetime(2026, 1, 1)
    # A single JSON value exceeds the session's sort buffer, as real chat history
    # and screenshot-bearing records do. Do not replace it with a mocked row.
    content = "chat-history-" * 32768
    rows = [record("BIG-" + uuid4().hex, "task", created + timedelta(seconds=i),
                   dict(id=f"large-{i}", ownerId=owner,
                        messages=[dict(role="assistant", content=content)]), project)
            for i in range(3)]
    save(rows)

    # Prove this data and session reproduce the old full-record ORDER BY query.
    # Without this control, a passing list could simply lack enough JSON to fail.
    with engine.connect() as connection:
        with pytest.raises(OperationalError) as failure:
            connection.execute(select(storage.Record).where(
                storage.Record.kind == "task", storage.Record.project_id == project
            ).order_by(storage.Record.created_at.desc())).all()
        assert failure.value.orig.args[0] == 1038

    with project_scope(project), user_scope(owner):
        values = storage.list_records("task")
    assert [value["id"] for value in values] == ["large-2", "large-1", "large-0"]
    assert all(value["messages"][0]["content"] == content for value in values)
    assert all(value["projectId"] == project for value in values)


def test_listing_preserves_order_across_batches_and_timestamp_ties(listing_store):
    save, _ = listing_store
    project = "P-" + uuid4().hex
    prefix = "ORDER-" + uuid4().hex[:12]
    created = datetime(2026, 2, 1)
    rows = [record(f"{prefix}-{i:04d}", "case", created + timedelta(seconds=i % 3),
                   dict(id=f"{prefix}-{i:04d}", ordinal=i), project)
            for i in range(507)]
    # Insert in a different order from the required created_at DESC, id DESC.
    save(rows[::2] + rows[1::2])
    expected = [row["id"] for row in sorted(
        rows, key=lambda row: (row["created_at"], row["id"]), reverse=True)]

    with project_scope(project):
        first = storage.list_records("case")
        second = storage.list_records("case")
    assert [value["id"] for value in first] == expected
    assert [value["id"] for value in second] == expected
    assert len({value["id"] for value in first}) == 507
    assert all(value["projectId"] == project for value in first)


@pytest.mark.parametrize("kind, owner_field", [
    ("task", "ownerId"), ("network_session", "accountId"),
    ("model_profile", "ownerId"), ("project", "ownerId"),
])
def test_listing_keeps_project_and_account_boundaries(listing_store, kind, owner_field):
    save, _ = listing_store
    prefix = "SCOPE-" + uuid4().hex[:12]
    project, other_project = prefix + "-P", prefix + "-Q"
    owner, other_owner = prefix + "-U", prefix + "-V"
    created = datetime(2026, 3, 1)
    own_id, private_id, foreign_id = (prefix + suffix for suffix in ("-A", "-B", "-C"))
    rows = [record(own_id, kind, created, dict(id=own_id, **{owner_field: owner}), project),
            record(private_id, kind, created + timedelta(seconds=2),
                   dict(id=private_id, **{owner_field: other_owner}), project)]
    if kind in {"task", "network_session"}:
        rows.append(record(foreign_id, kind, created + timedelta(seconds=1),
                           dict(id=foreign_id, **{owner_field: owner}), other_project))
    elif kind == "project":
        # Project membership grants visibility without making the user the owner;
        # list_records must still supplement the record ID when absent in JSON.
        rows.append(record(foreign_id, kind, created + timedelta(seconds=1),
                           dict(ownerId=other_owner, members=[owner]), other_project))
    save(rows)

    with project_scope(project), user_scope(owner):
        values = [value for value in storage.list_records(kind)
                  if value["id"].startswith(prefix)]
    assert [value["id"] for value in values] == (
        [foreign_id, own_id] if kind == "project" else [own_id])
    if kind in {"task", "network_session"}:
        assert values[0]["projectId"] == project

    with project_scope(project), user_scope(other_owner):
        values = [value for value in storage.list_records(kind)
                  if value["id"].startswith(prefix)]
    assert [value["id"] for value in values] == (
        [private_id, foreign_id] if kind == "project" else [private_id])
