"""Leitura da view `triage.publishable_proposals` e de `catalog.projects` (RF22).

Só faz `SELECT`: o publisher nunca escreve nos schemas `triage` e `catalog`. Recebe a
conexão de fora para que a leitura caia na mesma transação da tentativa.
"""

from psycopg import AsyncConnection
from psycopg.rows import class_row

from issue_publisher.domain.publish import PublishableProposal

_SELECT_PROPOSAL = """
SELECT proposal_id, repository, issue_number, origin_event_id, revision, current_attempt,
       case_status, kind, validation_status, comment_body
FROM triage.publishable_proposals
WHERE proposal_id = %s
"""
_SELECT_CATALOG = "SELECT 1 FROM catalog.projects WHERE repository = %s"


class PostgresProposalReader:
    """Implementação de `ProposalReader` sobre uma conexão assíncrona do psycopg."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get_publishable_proposal(self, proposal_id: int) -> PublishableProposal | None:
        async with self._conn.cursor(row_factory=class_row(PublishableProposal)) as cursor:
            await cursor.execute(_SELECT_PROPOSAL, (proposal_id,))
            return await cursor.fetchone()

    async def repository_in_catalog(self, repository: str) -> bool:
        cursor = await self._conn.execute(_SELECT_CATALOG, (repository,))
        return await cursor.fetchone() is not None
