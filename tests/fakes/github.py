"""Porta falsa do GitHub em memória (nenhum teste chama o GitHub de verdade).

Guarda os comentários por issue, aceita respostas programadas por chamada e conta os POSTs.
"""

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime

from issue_publisher.domain.github import GitHubComment, GitHubError


@dataclass(frozen=True)
class StoredComment:
    """Comentário guardado pela porta falsa."""

    id: int
    html_url: str
    created_at: datetime
    author: str
    body: str

    def to_comment(self) -> GitHubComment:
        return GitHubComment(id=self.id, html_url=self.html_url, created_at=self.created_at)


@dataclass(frozen=True)
class CreatedThenFail:
    """Resposta programada: o comentário é criado, mas a chamada falha (ex.: timeout no POST)."""

    error: GitHubError


CreateResponse = GitHubError | CreatedThenFail | None
"""`None` cria normalmente; `GitHubError` falha sem criar; `CreatedThenFail` cria e falha."""


class FakeGitHubIssues:
    """Implementação em memória de `GitHubIssuesPort`."""

    def __init__(self, bot_login: str = "triage-bot") -> None:
        self.bot_login = bot_login
        self.comments: dict[tuple[str, int], list[StoredComment]] = defaultdict(list)
        self.create_responses: deque[CreateResponse] = deque()
        self.find_responses: deque[GitHubError | None] = deque()
        self.post_count = 0
        self.find_count = 0
        self.posts: list[tuple[str, int, str]] = []
        self._ids = itertools.count(1000)

    # Programação das respostas -------------------------------------------------------------

    def program_create(self, *responses: CreateResponse) -> None:
        """Enfileira as respostas dos próximos POSTs; sem resposta enfileirada, cria normalmente."""
        self.create_responses.extend(responses)

    def program_find(self, *responses: GitHubError | None) -> None:
        """Enfileira as respostas das próximas buscas; `None` busca normalmente."""
        self.find_responses.extend(responses)

    def add_comment(
        self, repository: str, issue_number: int, body: str, author: str
    ) -> GitHubComment:
        """Coloca um comentário na issue (por exemplo, um marcador colado por outro autor)."""
        return self._store(repository, issue_number, body, author).to_comment()

    def comments_on(self, repository: str, issue_number: int) -> list[StoredComment]:
        return list(self.comments[(repository, issue_number)])

    # GitHubIssuesPort ----------------------------------------------------------------------

    async def identify_account(self) -> str:
        return self.bot_login

    async def find_comment_by_marker(
        self, repository: str, issue_number: int, marker: str, author: str
    ) -> GitHubComment | None:
        self.find_count += 1
        if self.find_responses:
            programmed = self.find_responses.popleft()
            if programmed is not None:
                raise programmed
        for comment in self.comments[(repository, issue_number)]:
            if comment.author == author and marker in comment.body:
                return comment.to_comment()
        return None

    async def create_comment(self, repository: str, issue_number: int, body: str) -> GitHubComment:
        self.post_count += 1
        self.posts.append((repository, issue_number, body))
        programmed = self.create_responses.popleft() if self.create_responses else None
        if isinstance(programmed, GitHubError):
            raise programmed
        stored = self._store(repository, issue_number, body, self.bot_login)
        if isinstance(programmed, CreatedThenFail):
            raise programmed.error
        return stored.to_comment()

    def _store(self, repository: str, issue_number: int, body: str, author: str) -> StoredComment:
        comment_id = next(self._ids)
        stored = StoredComment(
            id=comment_id,
            html_url=f"https://github.com/{repository}/issues/{issue_number}#issuecomment-{comment_id}",
            created_at=datetime.now(UTC),
            author=author,
            body=body,
        )
        self.comments[(repository, issue_number)].append(stored)
        return stored
