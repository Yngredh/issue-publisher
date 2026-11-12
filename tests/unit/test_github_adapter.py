"""Adaptador httpx do GitHub, testado com respx (nenhuma chamada real ao GitHub)."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
import structlog

from issue_publisher.adapters.github import GITHUB_API_VERSION, HttpxGitHubIssues
from issue_publisher.config import Settings
from issue_publisher.domain.github import ErrorClass, GitHubComment, GitHubError

API = "https://api.github.test"
TOKEN = "ghp_test_token_never_logged"
REPO = "acme/payments-api"
ISSUE = 42
COMMENTS_PATH = f"/repos/{REPO}/issues/{ISSUE}/comments"
MARKER = "<!-- triage:evt-1:1 -->"
BOT = "triage-bot"


def comment(comment_id: int, login: str, body: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-{comment_id}",
        "created_at": "2026-11-12T10:00:00Z",
        "user": {"login": login},
        "body": body,
    }


@pytest.fixture
async def client() -> AsyncIterator[HttpxGitHubIssues]:
    async with HttpxGitHubIssues(token=TOKEN, api_url=API) as github:
        yield github


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


async def test_headers_sent(client: HttpxGitHubIssues, api: respx.MockRouter) -> None:
    route = api.get("/user").respond(200, json={"login": BOT})

    assert await client.identify_account() == BOT

    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION == "2022-11-28"


async def test_configured_bot_login_skips_get_user(api: respx.MockRouter) -> None:
    route = api.get("/user").respond(200, json={"login": "other"})

    async with HttpxGitHubIssues(token=TOKEN, api_url=API, bot_login=BOT) as github:
        assert await github.identify_account() == BOT

    assert not route.called


def test_from_settings_uses_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir("/")
    settings = Settings(
        DATABASE_URL="postgresql://u:p@localhost/db",
        RABBITMQ_URL="amqp://guest:guest@localhost/",
        GITHUB_TOKEN=TOKEN,
        GITHUB_API_URL=API,
        GITHUB_CONNECT_TIMEOUT_S=3,
        GITHUB_READ_TIMEOUT_S=7,
    )

    github = HttpxGitHubIssues.from_settings(settings)

    timeout = github._client.timeout
    assert timeout.connect == 3
    assert timeout.read == 7
    assert str(github._client.base_url).rstrip("/") == API


async def test_marker_found_on_last_page(client: HttpxGitHubIssues, api: respx.MockRouter) -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        assert request.url.params["per_page"] == "100"
        if page == 1:
            body = [comment(1, BOT, "outro comentário"), comment(2, "alice", "oi")]
            next_url = f"{API}{COMMENTS_PATH}?per_page=100&page=2"
            last_url = f"{API}{COMMENTS_PATH}?per_page=100&page=3"
            link = f'<{next_url}>; rel="next", <{last_url}>; rel="last"'
            return httpx.Response(200, json=body, headers={"Link": link})
        if page == 2:
            next_url = f"{API}{COMMENTS_PATH}?per_page=100&page=3"
            link = f'<{next_url}>; rel="next", <{next_url}>; rel="last"'
            return httpx.Response(200, json=[comment(3, "bob", "x")], headers={"Link": link})
        return httpx.Response(200, json=[comment(4, BOT, f"Triagem\n\n{MARKER}")])

    route = api.get(COMMENTS_PATH).mock(side_effect=pages)

    found = await client.find_comment_by_marker(REPO, ISSUE, MARKER, BOT)

    assert found == GitHubComment(
        id=4,
        html_url=f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-4",
        created_at=datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
    )
    assert route.call_count == 3


async def test_marker_from_other_author_is_ignored(
    client: HttpxGitHubIssues, api: respx.MockRouter
) -> None:
    api.get(COMMENTS_PATH).respond(200, json=[comment(9, "mallory", f"copiado {MARKER}")])

    assert await client.find_comment_by_marker(REPO, ISSUE, MARKER, BOT) is None


async def test_other_marker_is_ignored(client: HttpxGitHubIssues, api: respx.MockRouter) -> None:
    api.get(COMMENTS_PATH).respond(200, json=[comment(9, BOT, "<!-- triage:evt-1:2 -->")])

    assert await client.find_comment_by_marker(REPO, ISSUE, MARKER, BOT) is None


async def test_find_error_is_classified(client: HttpxGitHubIssues, api: respx.MockRouter) -> None:
    api.get(COMMENTS_PATH).respond(503, json={"message": "Service Unavailable"})

    with pytest.raises(GitHubError) as raised:
        await client.find_comment_by_marker(REPO, ISSUE, MARKER, BOT)

    assert raised.value.temporary
    assert raised.value.last_error == "http_503: Service Unavailable"


async def test_create_comment_success(client: HttpxGitHubIssues, api: respx.MockRouter) -> None:
    route = api.post(COMMENTS_PATH).respond(201, json=comment(77, BOT, "corpo"))

    created = await client.create_comment(REPO, ISSUE, "corpo")

    assert created.id == 77
    assert created.html_url.endswith("#issuecomment-77")
    assert created.created_at == datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    assert route.calls.last.request.content == b'{"body":"corpo"}'


@pytest.mark.parametrize(
    ("status", "headers", "message", "expected", "retry_after_s"),
    [
        (500, {}, "Server Error", ErrorClass.TEMPORARY, None),
        (502, {}, "Bad Gateway", ErrorClass.TEMPORARY, None),
        (503, {}, "Service Unavailable", ErrorClass.TEMPORARY, None),
        (429, {"Retry-After": "30"}, "Too Many Requests", ErrorClass.TEMPORARY, 30),
        (429, {}, "Too Many Requests", ErrorClass.TEMPORARY, None),
        (403, {"Retry-After": "12"}, "secondary rate limit", ErrorClass.TEMPORARY, 12),
        (
            403,
            {"X-RateLimit-Remaining": "0"},
            "API rate limit exceeded",
            ErrorClass.TEMPORARY,
            None,
        ),
        (401, {}, "Bad credentials", ErrorClass.PERMANENT, None),
        (403, {}, "Resource not accessible", ErrorClass.PERMANENT, None),
        (403, {}, "Unable to create comment because issue is locked.", ErrorClass.PERMANENT, None),
        (404, {}, "Not Found", ErrorClass.PERMANENT, None),
        (410, {}, "Issues are disabled", ErrorClass.PERMANENT, None),
        (422, {}, "Validation Failed", ErrorClass.PERMANENT, None),
        (400, {}, "Problems parsing JSON", ErrorClass.PERMANENT, None),
    ],
)
async def test_create_comment_response_classes(
    client: HttpxGitHubIssues,
    api: respx.MockRouter,
    status: int,
    headers: dict[str, str],
    message: str,
    expected: ErrorClass,
    retry_after_s: float | None,
) -> None:
    api.post(COMMENTS_PATH).respond(status, json={"message": message}, headers=headers)

    with pytest.raises(GitHubError) as raised:
        await client.create_comment(REPO, ISSUE, "corpo")

    error = raised.value
    assert error.error_class is expected
    assert error.http_status == status
    assert error.retry_after_s == retry_after_s
    assert error.last_error == f"http_{status}: {message}"


async def test_error_without_json_uses_reason_phrase(
    client: HttpxGitHubIssues, api: respx.MockRouter
) -> None:
    api.post(COMMENTS_PATH).respond(502, text="<html>bad gateway</html>")

    with pytest.raises(GitHubError) as raised:
        await client.create_comment(REPO, ISSUE, "corpo")

    assert raised.value.last_error == "http_502: Bad Gateway"


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (httpx.ConnectTimeout("connect timeout"), "timeout: ConnectTimeout"),
        (httpx.ReadTimeout("read timeout"), "timeout: ReadTimeout"),
        (httpx.ConnectError("connection refused"), "connection_error: ConnectError"),
        (httpx.RemoteProtocolError("peer closed"), "connection_error: RemoteProtocolError"),
    ],
)
async def test_timeout_and_connection_errors_are_temporary(
    client: HttpxGitHubIssues,
    api: respx.MockRouter,
    exception: Exception,
    expected_error: str,
) -> None:
    api.post(COMMENTS_PATH).mock(side_effect=exception)

    with pytest.raises(GitHubError) as raised:
        await client.create_comment(REPO, ISSUE, "corpo")

    assert raised.value.temporary
    assert raised.value.http_status is None
    assert raised.value.last_error == expected_error
    assert raised.value.__cause__ is None


async def test_token_never_in_logs_or_errors(
    client: HttpxGitHubIssues, api: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    api.get("/user").respond(200, json={"login": BOT})
    api.get(COMMENTS_PATH).mock(side_effect=httpx.ConnectError(f"failed with {TOKEN}"))
    api.post(COMMENTS_PATH).respond(401, json={"message": f"Bad credentials {TOKEN}"})

    errors: list[GitHubError] = []
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        await client.identify_account()
        for call in (
            client.find_comment_by_marker(REPO, ISSUE, MARKER, BOT),
            client.create_comment(REPO, ISSUE, "corpo"),
        ):
            with pytest.raises(GitHubError) as raised:
                await call
            errors.append(raised.value)

    assert len(errors) == 2
    for error in errors:
        assert TOKEN not in str(error)
        assert TOKEN not in error.last_error
        assert TOKEN not in repr(error.args)
        assert error.__cause__ is None
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(logs)
    assert TOKEN not in repr(client)


async def test_error_detail_is_truncated_to_500_characters(
    client: HttpxGitHubIssues, api: respx.MockRouter
) -> None:
    long_message = "a" * 499 + "b" + "c" * 300
    api.post(COMMENTS_PATH).respond(422, json={"message": long_message})

    with pytest.raises(GitHubError) as raised:
        await client.create_comment(REPO, ISSUE, "corpo")

    assert raised.value.detail == long_message[:500]
    assert len(raised.value.detail) == 500
    assert raised.value.last_error == f"http_422: {'a' * 499}b"


async def test_token_is_redacted_before_truncation(api: respx.MockRouter) -> None:
    # O token começa antes do caractere 500: nenhum pedaço dele pode sobrar no detalhe.
    message = "x" * 490 + TOKEN + "y" * 100
    api.post(COMMENTS_PATH).respond(401, json={"message": message})

    async with HttpxGitHubIssues(token=TOKEN, api_url=API) as github:
        with pytest.raises(GitHubError) as raised:
            await github.create_comment(REPO, ISSUE, "corpo")

    assert raised.value.detail == ("x" * 490 + "***" + "y" * 100)[:500]
    assert TOKEN[:5] not in raised.value.detail


async def test_bot_login_is_compared_case_insensitively(api: respx.MockRouter) -> None:
    api.get(COMMENTS_PATH).respond(200, json=[comment(5, "triage-bot", f"Triagem\n\n{MARKER}")])

    async with HttpxGitHubIssues(token=TOKEN, api_url=API, bot_login="Triage-BOT") as github:
        account = await github.identify_account()
        found = await github.find_comment_by_marker(REPO, ISSUE, MARKER, account)

    assert account == "Triage-BOT"
    assert found is not None
    assert found.id == 5


async def test_search_stops_at_first_matching_comment(
    client: HttpxGitHubIssues, api: respx.MockRouter
) -> None:
    api.get(COMMENTS_PATH).respond(200, json=[comment(6, BOT, MARKER), comment(7, BOT, MARKER)])

    found = await client.find_comment_by_marker(REPO, ISSUE, MARKER, BOT)

    assert found is not None
    assert found.id == 6
