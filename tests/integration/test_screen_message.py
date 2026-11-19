"""Passos 1 a 3 com a view do stub (D11): nenhum caso cria linha em `publications`."""

from collections.abc import Callable

import psycopg
import pytest
from psycopg import AsyncConnection

from issue_publisher.adapters.proposals import PostgresProposalReader
from issue_publisher.domain.publish import ScreenOutcome, ScreenResult, screen_message
from tests.conftest import SeededProposal


async def screen(database_url: str, body: bytes | str) -> ScreenResult:
    async with await AsyncConnection.connect(database_url) as conn:
        return await screen_message(body, PostgresProposalReader(conn))


def publications_count(database_url: str) -> int:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT count(*) FROM publisher.publications").fetchone()[0]


def body(proposal_id: int, revision: int) -> str:
    return f'{{"proposalId": {proposal_id}, "revision": {revision}}}'


async def test_reader_returns_view_row(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal(revision=2, current_attempt=3)
    async with await AsyncConnection.connect(database_url) as conn:
        proposal = await PostgresProposalReader(conn).get_publishable_proposal(seeded.proposal_id)
    assert proposal is not None
    assert proposal.proposal_id == seeded.proposal_id
    assert proposal.repository == seeded.repository
    assert proposal.issue_number == seeded.issue_number
    assert proposal.origin_event_id == seeded.origin_event_id
    assert proposal.revision == 2
    assert proposal.current_attempt == 3
    assert proposal.case_status == "READY_TO_PUBLISH"
    assert proposal.kind == "triage"
    assert proposal.validation_status == "VALID"
    assert proposal.comment_body == seeded.comment_body


async def test_reader_returns_none_for_missing_proposal(database_url: str) -> None:
    async with await AsyncConnection.connect(database_url) as conn:
        assert await PostgresProposalReader(conn).get_publishable_proposal(-1) is None


async def test_reader_checks_catalog(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    inside = make_committed_proposal()
    outside = make_committed_proposal(repository="acme/not-in-catalog", in_catalog=False)
    async with await AsyncConnection.connect(database_url) as conn:
        reader = PostgresProposalReader(conn)
        assert await reader.repository_in_catalog(inside.repository) is True
        assert await reader.repository_in_catalog(outside.repository) is False


@pytest.mark.parametrize(
    "raw",
    ['{"proposalId": 1}', '{"proposalId": 1, "revision": 0}', "not json"],
    ids=["missing_revision", "revision_zero", "not_json"],
)
async def test_invalid_schema_is_discarded(database_url: str, raw: str) -> None:
    before = publications_count(database_url)
    result = await screen(database_url, raw)
    assert result.outcome is ScreenOutcome.DISCARDED_INVALID
    assert publications_count(database_url) == before


async def test_missing_proposal_is_discarded(database_url: str) -> None:
    before = publications_count(database_url)
    result = await screen(database_url, body(999_999_999, 1))
    assert result.outcome is ScreenOutcome.DISCARDED_INVALID
    assert publications_count(database_url) == before


async def test_revision_mismatch_is_discarded(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal(revision=1)
    before = publications_count(database_url)
    result = await screen(database_url, body(seeded.proposal_id, 2))
    assert result.outcome is ScreenOutcome.DISCARDED_INVALID
    assert publications_count(database_url) == before


async def test_stale_revision_is_skipped(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal(revision=1, current_attempt=2)
    before = publications_count(database_url)
    result = await screen(database_url, body(seeded.proposal_id, 1))
    assert result.outcome is ScreenOutcome.SKIPPED_STALE
    assert publications_count(database_url) == before


async def test_current_revision_proceeds_without_writing(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    seeded = make_committed_proposal(revision=2, current_attempt=2)
    before = publications_count(database_url)
    result = await screen(database_url, body(seeded.proposal_id, 2))
    assert result.outcome is ScreenOutcome.PROCEED
    assert result.proposal is not None
    assert result.proposal.proposal_id == seeded.proposal_id
    assert publications_count(database_url) == before
