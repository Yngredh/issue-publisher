"""Mensagem da fila `publish` e passos 1 a 3 do processamento (RF22, RNF07).

Os passos 1 a 3 decidem, antes de tocar em `publisher.publications`, se a mensagem segue
para a reivindicação, é descartada (schema inválido, proposta inexistente ou `revision`
diferente) ou é obsoleta (`revision < current_attempt`). Nenhum deles grava no banco.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

logger = structlog.get_logger()


class PublishMessage(BaseModel):
    """Mensagem `{"proposalId": 5, "revision": 1}` da fila `publish`.

    Só identifica a proposta: repositório, issue, corpo e estado do caso vêm sempre da view
    `triage.publishable_proposals`, nunca da mensagem.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: StrictInt = Field(alias="proposalId")
    revision: Annotated[StrictInt, Field(ge=1)]


@dataclass(frozen=True)
class PublishableProposal:
    """Linha da view `triage.publishable_proposals` ("Contratos > Leitura da proposta")."""

    proposal_id: int
    repository: str
    issue_number: int
    origin_event_id: str
    revision: int
    current_attempt: int
    case_status: str
    kind: str
    validation_status: str
    comment_body: str | None


class ProposalReader(Protocol):
    """Leitura da proposta e do catálogo; somente leitura, sem escrever em `triage`/`catalog`."""

    async def get_publishable_proposal(self, proposal_id: int) -> PublishableProposal | None:
        """Proposta da view pelo `proposal_id`, ou `None` se não existir."""
        ...

    async def repository_in_catalog(self, repository: str) -> bool:
        """Se o repositório está em `catalog.projects`."""
        ...


class ScreenOutcome(StrEnum):
    """Resultado dos passos 1 a 3."""

    DISCARDED_INVALID = "discarded_invalid"
    SKIPPED_STALE = "skipped_stale"
    PROCEED = "proceed"


@dataclass(frozen=True)
class ScreenResult:
    """Resultado dos passos 1 a 3; `proposal` só vem preenchida quando o passo 4 deve seguir."""

    outcome: ScreenOutcome
    message: PublishMessage | None = None
    proposal: PublishableProposal | None = None


def parse_message(body: bytes | str) -> PublishMessage | None:
    """Passo 1: valida o schema; JSON ou campos inválidos devolvem `None`."""
    try:
        return PublishMessage.model_validate_json(body)
    except ValidationError:
        return None


async def screen_message(body: bytes | str, reader: ProposalReader) -> ScreenResult:
    """Passos 1 a 3 do processamento, sem broker e sem escrever no banco.

    1. Schema inválido → descartar (`publish.discarded_invalid`).
    2. Proposta inexistente ou `revision` diferente da view → descartar.
    3. `revision < current_attempt` → obsoleta (`publish.skipped_stale`).
    """
    message = parse_message(body)
    if message is None:
        logger.info("publish.discarded_invalid")
        return ScreenResult(ScreenOutcome.DISCARDED_INVALID)

    proposal = await reader.get_publishable_proposal(message.proposal_id)
    if proposal is None or proposal.revision != message.revision:
        logger.info("publish.discarded_invalid", revision=message.revision)
        return ScreenResult(ScreenOutcome.DISCARDED_INVALID, message=message)

    log = logger.bind(
        event_id=proposal.origin_event_id,
        repository=proposal.repository,
        issue_number=proposal.issue_number,
        revision=proposal.revision,
    )
    if proposal.revision < proposal.current_attempt:
        log.info("publish.skipped_stale", current_attempt=proposal.current_attempt)
        return ScreenResult(ScreenOutcome.SKIPPED_STALE, message=message, proposal=proposal)

    return ScreenResult(ScreenOutcome.PROCEED, message=message, proposal=proposal)
