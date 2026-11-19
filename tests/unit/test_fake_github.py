"""Porta falsa do GitHub usada pelos testes das próximas etapas."""

import pytest

from issue_publisher.domain.github import ErrorClass, GitHubError, GitHubIssuesPort
from tests.fakes.github import CreatedThenFail, FakeGitHubIssues

REPO = "acme/payments-api"
MARKER = "<!-- triage:evt-1:1 -->"


def temporary() -> GitHubError:
    return GitHubError(ErrorClass.TEMPORARY, "http_503", "Service Unavailable", http_status=503)


async def test_fake_implements_port() -> None:
    port: GitHubIssuesPort = FakeGitHubIssues(bot_login="bot")

    assert await port.identify_account() == "bot"


async def test_create_then_find_by_marker() -> None:
    github = FakeGitHubIssues()

    created = await github.create_comment(REPO, 1, f"texto {MARKER}")
    found = await github.find_comment_by_marker(REPO, 1, MARKER, github.bot_login)

    assert found == created
    assert github.post_count == 1
    assert await github.find_comment_by_marker(REPO, 2, MARKER, github.bot_login) is None


async def test_marker_from_other_author_is_ignored() -> None:
    github = FakeGitHubIssues()
    github.add_comment(REPO, 1, MARKER, author="mallory")

    assert await github.find_comment_by_marker(REPO, 1, MARKER, github.bot_login) is None


async def test_programmed_responses_per_call() -> None:
    github = FakeGitHubIssues()
    github.program_create(temporary(), CreatedThenFail(temporary()), None)

    with pytest.raises(GitHubError):
        await github.create_comment(REPO, 1, MARKER)
    assert github.comments_on(REPO, 1) == []

    with pytest.raises(GitHubError):
        await github.create_comment(REPO, 1, MARKER)
    assert len(github.comments_on(REPO, 1)) == 1

    await github.create_comment(REPO, 1, MARKER)
    assert github.post_count == 3
    assert len(github.comments_on(REPO, 1)) == 2


async def test_programmed_find_error() -> None:
    github = FakeGitHubIssues()
    github.program_find(temporary())

    with pytest.raises(GitHubError):
        await github.find_comment_by_marker(REPO, 1, MARKER, github.bot_login)
    assert await github.find_comment_by_marker(REPO, 1, MARKER, github.bot_login) is None
    assert github.find_count == 2
