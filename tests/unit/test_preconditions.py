"""Regras do passo 6 com um leitor em memória."""

from dataclasses import replace

import pytest

from issue_publisher.domain.publication import (
    MAX_COMMENT_BODY_LENGTH,
    PreconditionCode,
    build_marker,
    check_preconditions,
)
from issue_publisher.domain.publish import PublishableProposal

MARKER = build_marker("evt-1", 2)
READY = PublishableProposal(
    proposal_id=1,
    repository="acme/payments-api",
    issue_number=7,
    origin_event_id="evt-1",
    revision=2,
    current_attempt=2,
    case_status="READY_TO_PUBLISH",
    kind="triage",
    validation_status="VALID",
    comment_body=f"Triagem.\n\n{MARKER}",
)


class Catalog:
    def __init__(self, *repositories: str) -> None:
        self.repositories = set(repositories)

    async def get_publishable_proposal(self, proposal_id: int) -> PublishableProposal | None:
        raise AssertionError("not used")

    async def repository_in_catalog(self, repository: str) -> bool:
        return repository in self.repositories


CATALOG = Catalog("acme/payments-api")


def test_marker_format() -> None:
    assert MARKER == "<!-- triage:evt-1:2 -->"


async def test_ready_proposal_passes() -> None:
    assert await check_preconditions(READY, CATALOG) is None


async def test_body_at_limit_passes() -> None:
    body = MARKER + "x" * (MAX_COMMENT_BODY_LENGTH - len(MARKER))
    assert await check_preconditions(replace(READY, comment_body=body), CATALOG) is None


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"validation_status": "INVALID"}, PreconditionCode.PROPOSAL_NOT_VALID),
        ({"comment_body": None}, PreconditionCode.PROPOSAL_NOT_VALID),
        ({"case_status": "NEEDS_REVIEW"}, PreconditionCode.CASE_NOT_READY),
        ({"repository": "acme/other"}, PreconditionCode.REPOSITORY_NOT_IN_CATALOG),
        ({"comment_body": ""}, PreconditionCode.INVALID_COMMENT_BODY),
        ({"comment_body": " \n\t "}, PreconditionCode.INVALID_COMMENT_BODY),
        (
            {"comment_body": MARKER + "x" * MAX_COMMENT_BODY_LENGTH},
            PreconditionCode.INVALID_COMMENT_BODY,
        ),
        ({"comment_body": "sem marcador"}, PreconditionCode.MARKER_MISMATCH),
        ({"comment_body": build_marker("evt-2", 2)}, PreconditionCode.MARKER_MISMATCH),
        ({"comment_body": build_marker("evt-1", 1)}, PreconditionCode.MARKER_MISMATCH),
        ({"comment_body": f"{MARKER}\n{MARKER}"}, PreconditionCode.MARKER_MISMATCH),
        (
            {"comment_body": f"{MARKER}\n{build_marker('evt-2', 2)}"},
            PreconditionCode.MARKER_MISMATCH,
        ),
        (
            {"comment_body": f"{build_marker('evt-1', 1)}\n{MARKER}"},
            PreconditionCode.MARKER_MISMATCH,
        ),
    ],
)
async def test_violations(changes: dict[str, object], code: PreconditionCode) -> None:
    violation = await check_preconditions(replace(READY, **changes), CATALOG)
    assert violation is not None
    assert violation.code is code
    assert violation.last_error.startswith(f"{code}: ")
