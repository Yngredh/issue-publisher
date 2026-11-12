"""Escrita em `publisher.publications` sobre uma conexão do psycopg (RNF03).

Recebe a conexão de fora: quem chama abre a transação que cobre a tentativa inteira, e o
`FOR UPDATE` da reivindicação segura a linha até o commit.
"""

from psycopg import AsyncConnection
from psycopg.rows import class_row

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
