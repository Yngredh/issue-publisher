"""Passos 7 a 9 com o PostgreSQL do testcontainers, o stub (D11) e a porta falsa do GitHub."""

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg
import pytest
import structlog
from psycopg import AsyncConnection

from issue_publisher.adapters.proposals import PostgresProposalReader
from issue_publisher.adapters.publications import PostgresPublicationStore
from issue_publisher.domain.github import ErrorClass, GitHubError, timeout_error
from issue_publisher.domain.publishing import (
    PublishOutcome,
    PublishResult,
    RetryPolicy,
    publish_proposal,
)
from tests.conftest import SeededProposal, marker
from tests.fakes.github import CreatedThenFail, FakeGitHubIssues

BOT = "triage-bot"
POLICY = RetryPolicy(max_tries=3, delays_s=(10, 20), retry_after_max_s=60)


class CrashBeforeCommit(Exception):
    """Simula a queda do processo depois do POST e antes do commit."""


async def run_publish(
    database_url: str,
    proposal_id: int,
    github: FakeGitHubIssues,
    *,
    crash_before_commit: bool = False,
) -> PublishResult:
    """Lê a proposta e roda os passos 4 a 9 numa transação, com commit ao fim."""
    async with await AsyncConnection.connect(database_url) as conn:
        async with conn.transaction():
            reader = PostgresProposalReader(conn)
            proposal = await reader.get_publishable_proposal(proposal_id)
            assert proposal is not None
            result = await publish_proposal(
                proposal,
                PostgresPublicationStore(conn),
                reader,
                github,
                account=BOT,
                policy=POLICY,
            )
            if crash_before_commit:
                raise CrashBeforeCommit
            return result


def publication_row(database_url: str, proposal_id: int) -> dict[str, object]:
    keys = (
        "id state tries failure_kind last_error github_comment_id github_comment_url published_at"
    ).split()
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(keys)} FROM publisher.publications WHERE proposal_id = %s",
            (proposal_id,),
        ).fetchall()
    assert len(rows) == 1
    return dict(zip(keys, rows[0], strict=True))


def tries_rows(database_url: str, publication_id: object) -> list[tuple[object, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT try_number, http_status, error FROM publisher.publication_tries "
            "WHERE publication_id = %s ORDER BY try_number",
            (publication_id,),
        ).fetchall()


def http_error(status: int, message: str) -> GitHubError:
    error_class = ErrorClass.TEMPORARY if status >= 500 else ErrorClass.PERMANENT
    return GitHubError(error_class, f"http_{status}", message, http_status=status)


async def test_ready_proposal_publishes_one_comment(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """Cenário 1."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.PUBLISHED
    comments = github.comments_on(seeded.repository, seeded.issue_number)
    assert len(comments) == 1
    assert comments[0].body == seeded.comment_body
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "PUBLISHED"
    assert row["tries"] == 1
    assert row["github_comment_id"] == comments[0].id
    assert row["github_comment_url"] == comments[0].html_url
    assert row["published_at"] == comments[0].created_at
    assert tries_rows(database_url, row["id"]) == [(1, 201, None)]
    assert github.find_count == 1
    assert github.post_count == 1


async def test_empty_description_publishes_template(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """Cenário 15: caso `READY_TO_PUBLISH` por descrição vazia, com o comentário-template."""
    template_event = "evt-empty-description"
    body = f"Faltam informações para analisar a issue.\n\n{marker(template_event, 1)}"
    seeded = make_committed_proposal(
        origin_event_id=template_event,
        kind="missing_info",
        status_reason="empty_description",
        comment_body=body,
    )
    github = FakeGitHubIssues(bot_login=BOT)

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.PUBLISHED
    assert github.posts == [(seeded.repository, seeded.issue_number, body)]
    assert publication_row(database_url, seeded.proposal_id)["state"] == "PUBLISHED"


async def test_marker_pasted_by_other_author_is_ignored(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """Cenário 12."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    pasted = github.add_comment(
        seeded.repository,
        seeded.issue_number,
        f"copiei {marker(seeded.origin_event_id, seeded.revision)}",
        author="mallory",
    )

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.PUBLISHED
    assert github.post_count == 1
    row = publication_row(database_url, seeded.proposal_id)
    assert row["github_comment_id"] != pasted.id
    assert len(github.comments_on(seeded.repository, seeded.issue_number)) == 2


async def test_crash_after_post_then_reprocess_adopts_comment(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """Cenário 5: queda depois do POST e antes do commit; o reprocessamento adota o comentário."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)

    with pytest.raises(CrashBeforeCommit):
        await run_publish(database_url, seeded.proposal_id, github, crash_before_commit=True)
    with psycopg.connect(database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM publisher.publications WHERE proposal_id = %s",
            (seeded.proposal_id,),
        ).fetchone()
    assert count == (0,)  # a transação foi desfeita

    with structlog.testing.capture_logs() as logs:
        result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.PUBLISHED
    comments = github.comments_on(seeded.repository, seeded.issue_number)
    assert len(comments) == 1
    assert github.post_count == 1
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "PUBLISHED"
    assert row["github_comment_id"] == comments[0].id
    assert row["tries"] == 0
    assert tries_rows(database_url, row["id"]) == []
    assert any(log["event"] == "publish.marker_found" for log in logs)


async def test_adopted_comment_keeps_its_created_at(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """D12: o comentário adotado grava `published_at` = `created_at` do comentário."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    existing = github.add_comment(
        seeded.repository, seeded.issue_number, str(seeded.comment_body), author="Triage-Bot"
    )

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.PUBLISHED
    assert github.post_count == 0
    row = publication_row(database_url, seeded.proposal_id)
    assert row["github_comment_id"] == existing.id
    assert row["github_comment_url"] == existing.html_url
    assert row["published_at"] == existing.created_at
    assert row["published_at"] < datetime.now(UTC)


async def test_timeout_after_post_is_retried_and_adopted(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """POST com timeout é temporário; a próxima tentativa começa pela busca e adota."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(CreatedThenFail(timeout_error("ReadTimeout")))

    first = await run_publish(database_url, seeded.proposal_id, github)
    second = await run_publish(database_url, seeded.proposal_id, github)

    assert first.outcome is PublishOutcome.RETRY
    assert first.delay_s == 10
    assert second.outcome is PublishOutcome.PUBLISHED
    assert github.post_count == 1
    row = publication_row(database_url, seeded.proposal_id)
    assert row["tries"] == 1
    assert row["last_error"] == "timeout: ReadTimeout"
    assert tries_rows(database_url, row["id"]) == [(1, None, "timeout: ReadTimeout")]


async def test_permission_403_fails_permanently_on_first_try(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """Cenário 9."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(http_error(403, "Resource not accessible by integration"))

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.FAILED
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "FAILED"
    assert row["failure_kind"] == "permanent"
    assert row["tries"] == 1
    assert row["last_error"] == "http_403: Resource not accessible by integration"
    assert tries_rows(database_url, row["id"]) == [(1, 403, None)]
    assert github.post_count == 1


async def test_search_timeout_does_not_consume_a_try(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """D12: busca com timeout não muda `tries` nem `publication_tries` e pede nova tentativa."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_find(timeout_error("ReadTimeout"))

    with structlog.testing.capture_logs() as logs:
        result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.RETRY
    assert result.delay_s == POLICY.delays_s[0]
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "PENDING"
    assert row["tries"] == 0
    assert row["last_error"] == "timeout: ReadTimeout"
    assert tries_rows(database_url, row["id"]) == []
    assert github.post_count == 0
    warnings = [log for log in logs if log["event"] == "publish.marker_search_failed"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"


async def test_search_failure_after_a_try_uses_the_try_delay(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """D12: com `tries = 2`, a falha na busca usa `PUBLISH_RETRY_DELAYS_S[1]`."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(http_error(503, "first"), http_error(502, "second"))
    await run_publish(database_url, seeded.proposal_id, github)
    await run_publish(database_url, seeded.proposal_id, github)
    github.program_find(http_error(500, "search down"))

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.RETRY
    assert result.delay_s == 20
    row = publication_row(database_url, seeded.proposal_id)
    assert row["tries"] == 2
    assert row["last_error"] == "http_500: search down"


async def test_search_404_fails_permanently_without_post(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """D12: busca com 404 vira `FAILED (permanent)` sem `POST`."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_find(http_error(404, "Not Found"))

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.FAILED
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "FAILED"
    assert row["failure_kind"] == "permanent"
    assert row["tries"] == 0
    assert row["last_error"] == "http_404: Not Found"
    assert tries_rows(database_url, row["id"]) == []
    assert github.post_count == 0


async def test_temporary_post_failures_update_last_error_and_exhaust(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """D12: `last_error` muda a cada falha; o `FAILED (retries_exhausted)` guarda a última."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(
        http_error(503, "first outage"),
        http_error(502, "second outage"),
        http_error(500, "third outage"),
    )

    first = await run_publish(database_url, seeded.proposal_id, github)
    assert first.outcome is PublishOutcome.RETRY
    assert first.delay_s == 10
    assert publication_row(database_url, seeded.proposal_id)["last_error"] == (
        "http_503: first outage"
    )

    second = await run_publish(database_url, seeded.proposal_id, github)
    assert second.outcome is PublishOutcome.RETRY
    assert second.delay_s == 20
    assert publication_row(database_url, seeded.proposal_id)["last_error"] == (
        "http_502: second outage"
    )

    third = await run_publish(database_url, seeded.proposal_id, github)
    assert third.outcome is PublishOutcome.FAILED
    row = publication_row(database_url, seeded.proposal_id)
    assert row["state"] == "FAILED"
    assert row["failure_kind"] == "retries_exhausted"
    assert row["tries"] == 3
    assert row["last_error"] == "http_500: third outage"
    assert tries_rows(database_url, row["id"]) == [(1, 503, None), (2, 502, None), (3, 500, None)]
    assert github.post_count == 3


async def test_retry_after_is_respected_in_the_delay(
    database_url: str, make_committed_proposal: Callable[..., SeededProposal]
) -> None:
    """D7: 429 com `Retry-After` maior que o atraso configurado usa o `Retry-After`."""
    seeded = make_committed_proposal()
    github = FakeGitHubIssues(bot_login=BOT)
    github.program_create(
        GitHubError(
            ErrorClass.TEMPORARY, "http_429", "Too Many Requests", http_status=429, retry_after_s=30
        )
    )

    result = await run_publish(database_url, seeded.proposal_id, github)

    assert result.outcome is PublishOutcome.RETRY
    assert result.delay_s == 30
    assert tries_rows(database_url, publication_row(database_url, seeded.proposal_id)["id"]) == [
        (1, 429, None)
    ]
