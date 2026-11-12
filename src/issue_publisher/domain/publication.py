"""Passos 4 a 6 do processamento: reivindicação, estado final e pré-condições (RF22, RNF03).

Tudo roda dentro da transação da tentativa, aberta por quem chama: a reivindicação insere a
linha de `publisher.publications` (se ainda não existir) e a trava com `FOR UPDATE` até o
commit. Nenhum destes passos chama o GitHub; os passos 7 a 9 continuam a partir de
`ClaimOutcome.READY`.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

from issue_publisher.domain.github import GitHubIssuesPort
from issue_publisher.domain.publish import ProposalReader, PublishableProposal

logger = structlog.get_logger()

MAX_COMMENT_BODY_LENGTH = 65_536
"""Limite do corpo do comentário em caracteres (regras do corpo da spec)."""


class PublicationState(StrEnum):
    """Valores de `publisher.publications.state`."""

    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class FailureKind(StrEnum):
    """Valores de `publisher.publications.failure_kind`."""

    RETRIES_EXHAUSTED = "retries_exhausted"
    PERMANENT = "permanent"


class PreconditionCode(StrEnum):
    """Códigos de pré-condição gravados em `last_error` (Issue Publisher Spec e D9)."""

    PROPOSAL_NOT_VALID = "proposal_not_valid"
    CASE_NOT_READY = "case_not_ready"
    REPOSITORY_NOT_IN_CATALOG = "repository_not_in_catalog"
    INVALID_COMMENT_BODY = "invalid_comment_body"
    MARKER_MISMATCH = "marker_mismatch"


@dataclass(frozen=True)
class Publication:
    """Linha de `publisher.publications` lida com `FOR UPDATE`."""

    id: int
    repository: str
    issue_number: int
    revision: int
    proposal_id: int
    origin_event_id: str
    kind: str
    state: str
    tries: int
    failure_kind: str | None
    last_error: str | None


class PublicationStore(Protocol):
    """Escrita em `publisher.publications` na transação da tentativa."""

    async def claim(self, proposal: PublishableProposal) -> Publication:
        """Passo 4: `INSERT ... ON CONFLICT DO NOTHING` e `SELECT ... FOR UPDATE` da linha."""
        ...

    async def mark_failed(
        self, publication_id: int, failure_kind: FailureKind, last_error: str
    ) -> None:
        """Grava `FAILED` com `failure_kind` e `last_error`."""
        ...


@dataclass(frozen=True)
class PreconditionViolation:
    """Pré-condição violada: vira `last_error` no formato `<código>: <detalhe>`."""

    code: PreconditionCode
    detail: str

    @property
    def last_error(self) -> str:
        return f"{self.code}: {self.detail}"


class ClaimOutcome(StrEnum):
    """Resultado dos passos 4 a 6."""

    ALREADY_FINAL = "already_final"
    PRECONDITION_FAILED = "precondition_failed"
    READY = "ready"


@dataclass(frozen=True)
class ClaimResult:
    """Resultado dos passos 4 a 6, com a linha reivindicada."""

    outcome: ClaimOutcome
    publication: Publication
    violation: PreconditionViolation | None = None


def build_marker(origin_event_id: str, revision: int) -> str:
    """Marcador oculto do comentário: `<!-- triage:<origin_event_id>:<revision> -->` (P2)."""
    return f"<!-- triage:{origin_event_id}:{revision} -->"


async def check_preconditions(
    proposal: PublishableProposal, reader: ProposalReader
) -> PreconditionViolation | None:
    """Passo 6 (RF22), na ordem do plano; devolve a primeira violação ou `None`.

    1. `validation_status = 'VALID'` e `comment_body` presente → senão `proposal_not_valid`.
    2. Caso em `READY_TO_PUBLISH` → senão `case_not_ready`.
    3. Repositório em `catalog.projects` → senão `repository_not_in_catalog`.
    4. Corpo não vazio e com até 65.536 caracteres → senão `invalid_comment_body`.
    5. Exatamente um marcador `<!-- triage:<origin_event_id>:<revision> -->` → senão
       `marker_mismatch`.
    """
    if proposal.validation_status != "VALID":
        return PreconditionViolation(
            PreconditionCode.PROPOSAL_NOT_VALID,
            f"validation_status is {proposal.validation_status}",
        )
    body = proposal.comment_body
    if body is None:
        return PreconditionViolation(PreconditionCode.PROPOSAL_NOT_VALID, "comment_body is null")
    if proposal.case_status != "READY_TO_PUBLISH":
        return PreconditionViolation(
            PreconditionCode.CASE_NOT_READY, f"case_status is {proposal.case_status}"
        )
    if not await reader.repository_in_catalog(proposal.repository):
        return PreconditionViolation(
            PreconditionCode.REPOSITORY_NOT_IN_CATALOG,
            f"{proposal.repository} is not in catalog.projects",
        )
    if body == "":
        return PreconditionViolation(PreconditionCode.INVALID_COMMENT_BODY, "comment_body is empty")
    if len(body) > MAX_COMMENT_BODY_LENGTH:
        return PreconditionViolation(
            PreconditionCode.INVALID_COMMENT_BODY,
            f"comment_body has {len(body)} characters (max {MAX_COMMENT_BODY_LENGTH})",
        )
    occurrences = body.count(build_marker(proposal.origin_event_id, proposal.revision))
    if occurrences != 1:
        return PreconditionViolation(
            PreconditionCode.MARKER_MISMATCH,
            f"expected exactly one marker for this event and revision, found {occurrences}",
        )
    return None


async def claim_and_check(
    proposal: PublishableProposal,
    store: PublicationStore,
    reader: ProposalReader,
    github: GitHubIssuesPort,
) -> ClaimResult:
    """Passos 4 a 6, dentro da transação aberta por quem chama, sem chamar o GitHub.

    `github` é a porta que os passos 7 a 9 usam a partir de `ClaimOutcome.READY`; aqui ela
    não é chamada em nenhum caso. Quem chama faz o commit e, só depois, o ack.
    """
    del github  # Os passos 4 a 6 nunca chamam o GitHub.
    log = logger.bind(
        event_id=proposal.origin_event_id,
        repository=proposal.repository,
        issue_number=proposal.issue_number,
        revision=proposal.revision,
    )

    publication = await store.claim(proposal)
    if publication.state in (PublicationState.PUBLISHED, PublicationState.FAILED):
        return ClaimResult(ClaimOutcome.ALREADY_FINAL, publication)

    violation = await check_preconditions(proposal, reader)
    if violation is not None:
        await store.mark_failed(publication.id, FailureKind.PERMANENT, violation.last_error)
        log.warning(
            "publish.failed",
            failure_kind=FailureKind.PERMANENT.value,
            last_error=violation.last_error,
        )
        return ClaimResult(ClaimOutcome.PRECONDITION_FAILED, publication, violation)

    return ClaimResult(ClaimOutcome.READY, publication)
