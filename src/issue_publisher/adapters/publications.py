"""Escrita em `publisher.publications` sobre uma conexão do psycopg (RNF03).

Recebe a conexão de fora: quem chama abre a transação que cobre a tentativa inteira, e o
`FOR UPDATE` da reivindicação segura a linha até o commit.
"""

from psycopg import AsyncConnection
from psycopg.rows import class_row

from issue_publisher.domain.github import GitHubComment
from issue_publisher.domain.publication import FailureKind, Publication, PublicationState
from issue_publisher.domain.publish import PublishableProposal

_CLAIM_INSERT = """
INSERT INTO publisher.publications
  (repository, issue_number, revision, proposal_id, origin_event_id, kind, state, tries)
VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
ON CONFLICT (repository, issue_number, revision) DO NOTHING
"""
_CLAIM_SELECT = """
SELECT id, repository, issue_number, revision, proposal_id, origin_event_id, kind, state,
       tries, failure_kind, last_error
FROM publisher.publications
WHERE repository = %s AND issue_number = %s AND revision = %s
FOR UPDATE
"""
_MARK_FAILED = """
UPDATE publisher.publications
SET state = %s, failure_kind = %s, last_error = %s, updated_at = now()
WHERE id = %s
"""
_MARK_PUBLISHED = """
UPDATE publisher.publications
SET state = %s, github_comment_id = %s, github_comment_url = %s, published_at = %s,
    updated_at = now()
WHERE id = %s
"""
_INSERT_TRY = """
INSERT INTO publisher.publication_tries (publication_id, try_number, http_status, error)
VALUES (%s, %s, %s, %s)
"""
_INCREMENT_TRIES = """
UPDATE publisher.publications SET tries = tries + 1, updated_at = now() WHERE id = %s
"""
_SET_LAST_ERROR = """
UPDATE publisher.publications SET last_error = %s, updated_at = now() WHERE id = %s
"""


class PostgresPublicationStore:
    """Implementação de `PublicationStore` sobre uma conexão assíncrona do psycopg."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def claim(self, proposal: PublishableProposal) -> Publication:
        await self._conn.execute(
            _CLAIM_INSERT,
            (
                proposal.repository,
                proposal.issue_number,
                proposal.revision,
                proposal.proposal_id,
                proposal.origin_event_id,
                proposal.kind,
                PublicationState.PENDING.value,
            ),
        )
        async with self._conn.cursor(row_factory=class_row(Publication)) as cursor:
            await cursor.execute(
                _CLAIM_SELECT, (proposal.repository, proposal.issue_number, proposal.revision)
            )
            publication = await cursor.fetchone()
        if publication is None:  # pragma: no cover - a linha acabou de ser garantida acima
            raise RuntimeError("claimed publication row not found")
        return publication

    async def mark_failed(
        self, publication_id: int, failure_kind: FailureKind, last_error: str
    ) -> None:
        await self._conn.execute(
            _MARK_FAILED,
            (PublicationState.FAILED.value, failure_kind.value, last_error, publication_id),
        )

    async def mark_published(self, publication_id: int, comment: GitHubComment) -> None:
        await self._conn.execute(
            _MARK_PUBLISHED,
            (
                PublicationState.PUBLISHED.value,
                comment.id,
                comment.html_url,
                comment.created_at,
                publication_id,
            ),
        )

    async def record_try(
        self, publication_id: int, try_number: int, http_status: int | None, error: str | None
    ) -> None:
        await self._conn.execute(_INSERT_TRY, (publication_id, try_number, http_status, error))
        await self._conn.execute(_INCREMENT_TRIES, (publication_id,))

    async def set_last_error(self, publication_id: int, last_error: str) -> None:
        await self._conn.execute(_SET_LAST_ERROR, (last_error, publication_id))
