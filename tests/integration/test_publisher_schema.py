"""Schema `publisher`, runner de migrações, adaptador de conexão e stub de `triage`/`catalog`."""

from collections.abc import Callable

import psycopg
import pytest
from psycopg import errors

from issue_publisher.adapters.database import close_pool, create_pool, open_pool
from issue_publisher.migrate import apply_migrations, discover_migrations
from tests.conftest import SeededProposal, marker

_INSERT_PUBLICATION = """
INSERT INTO publisher.publications
  (repository, issue_number, revision, proposal_id, origin_event_id, kind, state, tries,
   failure_kind, github_comment_id, github_comment_url)
VALUES (%(repository)s, %(issue_number)s, %(revision)s, 1, 'evt-1', 'triage', %(state)s,
        %(tries)s, %(failure_kind)s, %(github_comment_id)s, %(github_comment_url)s)
RETURNING id
"""
_COMMENT_URL = "https://github.com/acme/payments-api/issues/42#issuecomment-1"


def insert_publication(conn: psycopg.Connection, **overrides: object) -> int:
    params: dict[str, object] = {
        "repository": "acme/payments-api",
        "issue_number": 42,
        "revision": 1,
        "state": "PENDING",
        "tries": 0,
        "failure_kind": None,
        "github_comment_id": None,
        "github_comment_url": None,
    }
    params.update(overrides)
    return conn.execute(_INSERT_PUBLICATION, params).fetchone()[0]


def insert_try(conn: psycopg.Connection, publication_id: int, try_number: int) -> None:
    conn.execute(
        "INSERT INTO publisher.publication_tries (publication_id, try_number, http_status) "
        "VALUES (%s, %s, 503)",
        (publication_id, try_number),
    )


def assert_rejected(
    conn: psycopg.Connection, error: type[Exception], action: Callable[[], object]
) -> None:
    """Executa `action` num savepoint e confirma que o banco recusa com `error`."""
    with pytest.raises(error):
        with conn.transaction():
            action()


def test_runner_is_idempotent(database_url: str) -> None:
    assert apply_migrations(database_url) == []
    assert apply_migrations(database_url) == []
    with psycopg.connect(database_url) as conn:
        rows = conn.execute("SELECT version FROM publisher.schema_migrations ORDER BY 1")
        versions = [row[0] for row in rows]
    assert versions == [version for version, _ in discover_migrations()]
    assert versions == ["0001"]


def test_unique_key_repository_issue_revision(db_conn: psycopg.Connection) -> None:
    insert_publication(db_conn)
    assert_rejected(db_conn, errors.UniqueViolation, lambda: insert_publication(db_conn))
    # Outra revisão da mesma issue é outra publicação.
    insert_publication(db_conn, revision=2)


@pytest.mark.parametrize(
    "overrides",
    [{}, {"github_comment_id": 1}, {"github_comment_url": _COMMENT_URL}],
    ids=["no_id_no_url", "no_url", "no_id"],
)
def test_published_without_id_or_url_rejected(
    db_conn: psycopg.Connection, overrides: dict[str, object]
) -> None:
    assert_rejected(
        db_conn,
        errors.CheckViolation,
        lambda: insert_publication(db_conn, state="PUBLISHED", **overrides),
    )


def test_published_with_id_and_url_accepted(db_conn: psycopg.Connection) -> None:
    insert_publication(
        db_conn, state="PUBLISHED", github_comment_id=1, github_comment_url=_COMMENT_URL
    )


def test_failed_requires_failure_kind(db_conn: psycopg.Connection) -> None:
    assert_rejected(
        db_conn, errors.CheckViolation, lambda: insert_publication(db_conn, state="FAILED")
    )
    insert_publication(db_conn, state="FAILED", failure_kind="permanent")
    insert_publication(db_conn, revision=2, state="FAILED", failure_kind="retries_exhausted")


@pytest.mark.parametrize("tries", [-1, 4])
def test_tries_out_of_range_rejected(db_conn: psycopg.Connection, tries: int) -> None:
    assert_rejected(
        db_conn, errors.CheckViolation, lambda: insert_publication(db_conn, tries=tries)
    )


@pytest.mark.parametrize("tries", [0, 3])
def test_tries_bounds_accepted(db_conn: psycopg.Connection, tries: int) -> None:
    insert_publication(db_conn, tries=tries)


@pytest.mark.parametrize("try_number", [0, 4])
def test_try_number_out_of_range_rejected(db_conn: psycopg.Connection, try_number: int) -> None:
    publication_id = insert_publication(db_conn)
    assert_rejected(
        db_conn, errors.CheckViolation, lambda: insert_try(db_conn, publication_id, try_number)
    )


def test_try_number_unique_per_publication(db_conn: psycopg.Connection) -> None:
    publication_id = insert_publication(db_conn)
    for try_number in (1, 2, 3):
        insert_try(db_conn, publication_id, try_number)
    assert_rejected(db_conn, errors.UniqueViolation, lambda: insert_try(db_conn, publication_id, 3))


def test_stub_view_returns_proposal_columns(
    db_conn: psycopg.Connection, make_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_proposal(revision=2, current_attempt=3)

    cursor = db_conn.execute(
        "SELECT * FROM triage.publishable_proposals WHERE proposal_id = %s",
        (seeded.proposal_id,),
    )
    columns = [column.name for column in cursor.description]
    row = dict(zip(columns, cursor.fetchone(), strict=True))

    assert columns == [
        "proposal_id",
        "repository",
        "issue_number",
        "origin_event_id",
        "revision",
        "current_attempt",
        "case_status",
        "kind",
        "validation_status",
        "comment_body",
    ]
    assert row == {
        "proposal_id": seeded.proposal_id,
        "repository": seeded.repository,
        "issue_number": seeded.issue_number,
        "origin_event_id": seeded.origin_event_id,
        "revision": 2,
        "current_attempt": 3,
        "case_status": "READY_TO_PUBLISH",
        "kind": "triage",
        "validation_status": "VALID",
        "comment_body": seeded.comment_body,
    }
    assert row["comment_body"].count(marker(seeded.origin_event_id, 2)) == 1


def test_factory_can_leave_repository_out_of_catalog(
    db_conn: psycopg.Connection, make_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_proposal(repository="acme/unknown", in_catalog=False)
    found = db_conn.execute(
        "SELECT 1 FROM catalog.projects WHERE repository = %s", (seeded.repository,)
    ).fetchone()
    assert found is None


async def test_async_pool_connects(database_url: str) -> None:
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT count(*) FROM publisher.publications")
            assert await cursor.fetchone() is not None
    finally:
        await close_pool(pool)
