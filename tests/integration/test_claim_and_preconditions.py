"""Passos 4 a 6 com o PostgreSQL do testcontainers, o stub (D11) e a porta falsa do GitHub."""

from collections.abc import Callable

import psycopg
import pytest
from psycopg import AsyncConnection

from issue_publisher.adapters.proposals import PostgresProposalReader
from issue_publisher.adapters.publications import PostgresPublicationStore
from issue_publisher.domain.publication import (
    MAX_COMMENT_BODY_LENGTH,
    ClaimOutcome,
    ClaimResult,
    claim_and_check,
)
from tests.conftest import SeededProposal, marker
from tests.fakes.github import FakeGitHubIssues


async def run_steps_4_to_6(
    database_url: str, proposal_id: int, github: FakeGitHubIssues
) -> ClaimResult:
    """Lê a proposta e roda os passos 4 a 6 numa transação, com commit ao fim."""
    async with await AsyncConnection.connect(database_url) as conn:
        async with conn.transaction():
            reader = PostgresProposalReader(conn)
            proposal = await reader.get_publishable_proposal(proposal_id)
            assert proposal is not None
            return await claim_and_check(
                proposal, PostgresPublicationStore(conn), reader, github
            )


def publication_row(database_url: str, proposal_id: int) -> dict[str, object]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT repository, issue_number, revision, proposal_id, origin_event_id, kind, "
            "state, tries, failure_kind, last_error, github_comment_id "
            "FROM publisher.publications WHERE proposal_id = %s",
            (proposal_id,),
        ).fetchall()
    assert len(rows) == 1
    keys = (
        "repository issue_number revision proposal_id origin_event_id kind state tries "
        "failure_kind last_error github_comment_id"
    ).split()
    return dict(zip(keys, rows[0], strict=True))


def assert_github_untouched(github: FakeGitHubIssues) -> None:
    assert github.post_count == 0
    assert github.find_count == 0


async def test_ready_proposal_is_claimed_as_pending(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal(kind="missing_info")
    github = FakeGitHubIssues()

    result = await run_steps_4_to_6(database_url, seeded.proposal_id, github)

    assert result.outcome is ClaimOutcome.READY
    row = publication_row(database_url, seeded.proposal_id)
    assert row["repository"] == seeded.repository
    assert row["issue_number"] == seeded.issue_number
    assert row["revision"] == seeded.revision
    assert row["origin_event_id"] == seeded.origin_event_id
    assert row["kind"] == "missing_info"
    assert row["state"] == "PENDING"
    assert row["tries"] == 0
    assert row["failure_kind"] is None
    assert_github_untouched(github)


async def test_second_claim_reuses_the_same_row(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal()
    github = FakeGitHubIssues()

    first = await run_steps_4_to_6(database_url, seeded.proposal_id, github)
    second = await run_steps_4_to_6(database_url, seeded.proposal_id, github)

    assert first.publication.id == second.publication.id
    publication_row(database_url, seeded.proposal_id)  # exatamente uma linha


async def test_claim_locks_the_row_until_commit(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal()
    # Primeira entrega grava a linha; a segunda reivindica a linha já existente e a trava.
    await run_steps_4_to_6(database_url, seeded.proposal_id, FakeGitHubIssues())
    async with await AsyncConnection.connect(database_url) as conn:
        async with conn.transaction():
            reader = PostgresProposalReader(conn)
            proposal = await reader.get_publishable_proposal(seeded.proposal_id)
            assert proposal is not None
            result = await claim_and_check(
                proposal, PostgresPublicationStore(conn), reader, FakeGitHubIssues()
            )
            assert result.outcome is ClaimOutcome.READY

            async with await AsyncConnection.connect(database_url) as other:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await other.execute(
                        "SELECT id FROM publisher.publications WHERE id = %s FOR UPDATE NOWAIT",
                        (result.publication.id,),
                    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param(
            {"repository": "acme/outside-catalog", "in_catalog": False},
            "repository_not_in_catalog",
            id="scenario_11_repository_not_in_catalog",
        ),
        pytest.param(
            {"comment_body": "Triagem sem marcador."},
            "marker_mismatch",
            id="scenario_14_missing_marker",
        ),
        pytest.param(
            {"comment_body": f"Triagem.\n\n{marker('evt-other', 1)}"},
            "marker_mismatch",
            id="scenario_14_marker_of_other_event",
        ),
        pytest.param(
            {"comment_body": ""},
            "invalid_comment_body",
            id="empty_body",
        ),
        pytest.param(
            {"comment_body": "   \n\t  "},
            "invalid_comment_body",
            id="blank_body",
        ),
        pytest.param(
            {
                "origin_event_id": "evt-with-extra",
                "comment_body": f"Triagem.\n\n{marker('evt-with-extra', 1)}\n"
                f"{marker('evt-other', 1)}",
            },
            "marker_mismatch",
            id="other_event_marker_next_to_the_right_one",
        ),
        pytest.param(
            {
                "origin_event_id": "evt-twice",
                "comment_body": f"{marker('evt-twice', 1)}\n{marker('evt-twice', 1)}",
            },
            "marker_mismatch",
            id="same_marker_twice",
        ),
        pytest.param(
            {"validation_status": "INVALID", "comment_body": None},
            "proposal_not_valid",
            id="invalid_proposal",
        ),
        pytest.param(
            {"case_status": "NEEDS_REVIEW"},
            "case_not_ready",
            id="case_not_ready_to_publish",
        ),
    ],
)
async def test_precondition_violation_fails_permanently_without_github(
    database_url: str,
    make_committed_proposal: Callable[..., SeededProposal],
    overrides: dict[str, object],
    code: str,
) -> None:
    seeded = make_committed_proposal(**overrides)
    github = FakeGitHubIssues()

    result = await run_steps_4_to_6(database_url, seeded.proposal_id, github)

    assert result.outcome is ClaimOutcome.PRECONDITION_FAILED
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "FAILED"
    assert row["failure_kind"] == "permanent"
    assert row["tries"] == 0
    assert str(row["last_error"]).startswith(f"{code}: ")
    assert_github_untouched(github)


async def test_body_above_limit_fails_permanently_without_github(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal(origin_event_id="evt-too-long")
    body = marker("evt-too-long", 1)
    body += "x" * (MAX_COMMENT_BODY_LENGTH + 1 - len(body))
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE triage.proposals SET comment_body = %s WHERE id = %s",
            (body, seeded.proposal_id),
        )
    github = FakeGitHubIssues()

    result = await run_steps_4_to_6(database_url, seeded.proposal_id, github)

    assert result.outcome is ClaimOutcome.PRECONDITION_FAILED
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "FAILED"
    assert row["failure_kind"] == "permanent"
    assert str(row["last_error"]).startswith("invalid_comment_body: ")
    assert_github_untouched(github)


@pytest.mark.parametrize("state", ["PUBLISHED", "FAILED"])
async def test_final_row_ends_without_github(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal], state: str
) -> None:
    seeded = make_committed_proposal()
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO publisher.publications "
            "(repository, issue_number, revision, proposal_id, origin_event_id, kind, state, "
            " tries, failure_kind, last_error, github_comment_id, github_comment_url) "
            "VALUES (%s, %s, %s, %s, %s, 'triage', %s, 1, %s, %s, %s, %s)",
            (
                seeded.repository,
                seeded.issue_number,
                seeded.revision,
                seeded.proposal_id,
                seeded.origin_event_id,
                state,
                "permanent" if state == "FAILED" else None,
                "http_404: Not Found" if state == "FAILED" else None,
                42 if state == "PUBLISHED" else None,
                "https://github.com/x/y/issues/1#issuecomment-42" if state == "PUBLISHED" else None,
            ),
        )
    github = FakeGitHubIssues()

    result = await run_steps_4_to_6(database_url, seeded.proposal_id, github)

    assert result.outcome is ClaimOutcome.ALREADY_FINAL
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == state
    assert row["tries"] == 1
    assert_github_untouched(github)
