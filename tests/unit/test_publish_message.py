"""Modelo da mensagem `publish` e passos 1 a 3 com um leitor em memória."""

import json

import pytest
import structlog
from pydantic import ValidationError

from issue_publisher.domain.publish import (
    PublishableProposal,
    PublishMessage,
    ScreenOutcome,
    parse_message,
    screen_message,
)


def test_valid_message() -> None:
    message = PublishMessage.model_validate_json('{"proposalId": 5, "revision": 1}')
    assert message.proposal_id == 5
    assert message.revision == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"proposalId": 5, "revision": 1, "repository": "acme/x"},
        {"proposalId": 5, "revision": 0},
        {"proposalId": 5, "revision": -1},
        {"proposalId": 5},
        {"revision": 1},
        {"proposalId": "5", "revision": 1},
        {"proposalId": 5, "revision": "1"},
        {"proposalId": 5.5, "revision": 1},
        {"proposalId": None, "revision": 1},
        {"proposal_id": 5, "revision": 1},
        [5, 1],
    ],
    ids=[
        "extra_field",
        "revision_zero",
        "revision_negative",
        "missing_revision",
        "missing_proposal_id",
        "proposal_id_string",
        "revision_string",
        "proposal_id_float",
        "proposal_id_null",
        "snake_case_name",
        "array",
    ],
)
def test_invalid_message_rejected(payload: object) -> None:
    with pytest.raises(ValidationError):
        PublishMessage.model_validate_json(json.dumps(payload))
    assert parse_message(json.dumps(payload)) is None


@pytest.mark.parametrize("body", [b"", b"not json", b"\xff\xfe", b"{"])
def test_malformed_body_is_invalid(body: bytes) -> None:
    assert parse_message(body) is None


class FakeReader:
    def __init__(self, proposals: dict[int, PublishableProposal]) -> None:
        self.proposals = proposals
        self.lookups: list[int] = []

    async def get_publishable_proposal(self, proposal_id: int) -> PublishableProposal | None:
        self.lookups.append(proposal_id)
        return self.proposals.get(proposal_id)

    async def repository_in_catalog(self, repository: str) -> bool:
        return True


def make_proposal(revision: int = 1, current_attempt: int = 1) -> PublishableProposal:
    return PublishableProposal(
        proposal_id=5,
        repository="acme/payments-api",
        issue_number=42,
        origin_event_id="evt-1",
        revision=revision,
        current_attempt=current_attempt,
        case_status="READY_TO_PUBLISH",
        kind="triage",
        validation_status="VALID",
        comment_body="Triagem.\n\n<!-- triage:evt-1:1 -->",
    )


async def test_invalid_schema_is_discarded_without_reading() -> None:
    reader = FakeReader({})
    with structlog.testing.capture_logs() as logs:
        result = await screen_message(b'{"proposalId": 5}', reader)
    assert result.outcome is ScreenOutcome.DISCARDED_INVALID
    assert reader.lookups == []
    assert [log["event"] for log in logs] == ["publish.discarded_invalid"]


async def test_missing_proposal_is_discarded() -> None:
    result = await screen_message(b'{"proposalId": 5, "revision": 1}', FakeReader({}))
    assert result.outcome is ScreenOutcome.DISCARDED_INVALID
    assert result.proposal is None


async def test_revision_mismatch_is_discarded() -> None:
    reader = FakeReader({5: make_proposal(revision=1)})
    with structlog.testing.capture_logs() as logs:
        result = await screen_message(b'{"proposalId": 5, "revision": 2}', reader)
    assert result.outcome is ScreenOutcome.DISCARDED_INVALID
    assert result.proposal is None
    assert [log["event"] for log in logs] == ["publish.discarded_invalid"]


async def test_stale_revision_is_skipped() -> None:
    reader = FakeReader({5: make_proposal(revision=1, current_attempt=2)})
    with structlog.testing.capture_logs() as logs:
        result = await screen_message(b'{"proposalId": 5, "revision": 1}', reader)
    assert result.outcome is ScreenOutcome.SKIPPED_STALE
    assert logs[0]["event"] == "publish.skipped_stale"
    assert logs[0]["event_id"] == "evt-1"
    assert logs[0]["repository"] == "acme/payments-api"
    assert logs[0]["issue_number"] == 42
    assert logs[0]["revision"] == 1


async def test_current_revision_proceeds() -> None:
    proposal = make_proposal(revision=1, current_attempt=1)
    with structlog.testing.capture_logs() as logs:
        result = await screen_message(
            b'{"proposalId": 5, "revision": 1}', FakeReader({5: proposal})
        )
    assert result.outcome is ScreenOutcome.PROCEED
    assert result.proposal == proposal
    assert result.message == PublishMessage(proposalId=5, revision=1)
    assert logs == []
