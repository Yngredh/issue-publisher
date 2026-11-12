"""Passos 7 a 9 do processamento: busca do marcador, POST e resultado (RF23, RF24, RNF03).

Roda dentro da transação da tentativa, aberta por quem chama, depois dos passos 4 a 6
(`claim_and_check`). Quem chama faz o commit e, só depois, o ack; na "nova tentativa", o
envio da cópia para `publish.retry` fica com o consumidor (Etapa 7).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import structlog

from issue_publisher.config import Settings
from issue_publisher.domain.github import GitHubError, GitHubIssuesPort
from issue_publisher.domain.publication import (
    ClaimOutcome,
    FailureKind,
    PublicationStore,
    build_marker,
    claim_and_check,
)
from issue_publisher.domain.publish import ProposalReader, PublishableProposal

logger = structlog.get_logger()

CREATED_STATUS = 201

TRIES_ALREADY_AT_MAX_ERROR = "retries_exhausted: tries already at PUBLISH_MAX_TRIES"
"""`last_error` quando a linha já chega ao passo 8 com `tries >= PUBLISH_MAX_TRIES` (D14 P-b)."""


@dataclass(frozen=True)
class RetryPolicy:
    """Limite de tentativas e atrasos da nova tentativa (D7, D10 e D12)."""

    max_tries: int
    delays_s: Sequence[int]
    retry_after_max_s: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetryPolicy":
        return cls(
            max_tries=settings.PUBLISH_MAX_TRIES,
            delays_s=tuple(settings.PUBLISH_RETRY_DELAYS_S),
            retry_after_max_s=settings.PUBLISH_RETRY_AFTER_MAX_S,
        )

    def delay_after_try(self, tries: int, retry_after_s: float | None) -> float:
        """D7: `PUBLISH_RETRY_DELAYS_S[tries - 1]` depois da falha temporária número `tries`."""
        return self._apply_retry_after(self.delays_s[tries - 1], retry_after_s)

    def delay_after_search_failure(self, tries: int, retry_after_s: float | None) -> float:
        """D12: `PUBLISH_RETRY_DELAYS_S[0]` com `tries = 0`; senão `[tries - 1]`."""
        index = 0 if tries == 0 else tries - 1
        return self._apply_retry_after(self.delays_s[index], retry_after_s)

    def _apply_retry_after(self, configured_s: int, retry_after_s: float | None) -> float:
        """Maior entre o atraso configurado e o `Retry-After`, este limitado ao máximo (D7)."""
        if retry_after_s is None:
            return float(configured_s)
        return float(max(configured_s, min(retry_after_s, self.retry_after_max_s)))


class PublishOutcome(StrEnum):
    """Resultado do processamento de uma proposta (passos 4 a 9)."""

    ALREADY_FINAL = "already_final"
    PUBLISHED = "published"
    FAILED = "failed"
    RETRY = "retry"


@dataclass(frozen=True)
class PublishResult:
    """Resultado; `delay_s` só vem preenchido em `RETRY` (atraso da cópia em `publish.retry`)."""

    outcome: PublishOutcome
    publication_id: int
    delay_s: float | None = None
    last_error: str | None = None


async def publish_proposal(
    proposal: PublishableProposal,
    store: PublicationStore,
    reader: ProposalReader,
    github: GitHubIssuesPort,
    *,
    account: str,
    policy: RetryPolicy,
) -> PublishResult:
    """Passos 4 a 9 na transação aberta por quem chama; `account` é a conta do publisher."""
    claim = await claim_and_check(proposal, store, reader, github)
    publication = claim.publication
    if claim.outcome is ClaimOutcome.ALREADY_FINAL:
        return PublishResult(PublishOutcome.ALREADY_FINAL, publication.id)
    if claim.outcome is ClaimOutcome.PRECONDITION_FAILED:
        last_error = claim.violation.last_error if claim.violation else None
        return PublishResult(PublishOutcome.FAILED, publication.id, last_error=last_error)

    log = logger.bind(
        event_id=proposal.origin_event_id,
        repository=proposal.repository,
        issue_number=proposal.issue_number,
        revision=proposal.revision,
    )
    marker = build_marker(proposal.origin_event_id, proposal.revision)

    # Passo 7: busca do marcador antes de todo POST; não consome tentativa (D12).
    try:
        found = await github.find_comment_by_marker(
            proposal.repository, proposal.issue_number, marker, account
        )
    except GitHubError as error:
        if error.temporary:
            await store.set_last_error(publication.id, error.last_error)
            delay_s = policy.delay_after_search_failure(publication.tries, error.retry_after_s)
            log.warning(
                "publish.marker_search_failed",
                tries=publication.tries,
                http_status=error.http_status,
                delay_s=delay_s,
                last_error=error.last_error,
            )
            return PublishResult(
                PublishOutcome.RETRY, publication.id, delay_s=delay_s, last_error=error.last_error
            )
        return await _fail_with(store, log, publication.id, FailureKind.PERMANENT, error)

    if found is not None:
        await store.mark_published(publication.id, found)
        log.info("publish.marker_found", github_comment_id=found.id)
        return PublishResult(PublishOutcome.PUBLISHED, publication.id)

    # Passo 8: limite já atingido (limite reduzido com a linha `PENDING`) → sem POST (D14 P-b).
    if publication.tries >= policy.max_tries:
        return await _fail(
            store,
            log,
            publication.id,
            FailureKind.RETRIES_EXHAUSTED,
            TRIES_ALREADY_AT_MAX_ERROR,
            http_status=None,
        )

    # Passo 8: POST, linha em `publication_tries` e `tries` incrementado na mesma transação.
    try_number = publication.tries + 1
    log.info("publish.try", try_number=try_number)
    try:
        comment = await github.create_comment(
            proposal.repository, proposal.issue_number, proposal.comment_body or ""
        )
    except GitHubError as error:
        await store.record_try(
            publication.id,
            try_number,
            error.http_status,
            None if error.http_status is not None else error.last_error,
        )
        # Passo 9 para as falhas.
        if not error.temporary:
            return await _fail_with(
                store, log, publication.id, FailureKind.PERMANENT, error, try_number=try_number
            )
        if try_number >= policy.max_tries:
            return await _fail_with(
                store,
                log,
                publication.id,
                FailureKind.RETRIES_EXHAUSTED,
                error,
                try_number=try_number,
            )
        await store.set_last_error(publication.id, error.last_error)
        delay_s = policy.delay_after_try(try_number, error.retry_after_s)
        return PublishResult(
            PublishOutcome.RETRY, publication.id, delay_s=delay_s, last_error=error.last_error
        )

    # Passo 9 para o sucesso (201 Created).
    await store.record_try(publication.id, try_number, CREATED_STATUS, None)
    await store.mark_published(publication.id, comment)
    log.info("publish.published", try_number=try_number, github_comment_id=comment.id)
    return PublishResult(PublishOutcome.PUBLISHED, publication.id)


async def _fail_with(
    store: PublicationStore,
    log: structlog.typing.FilteringBoundLogger,
    publication_id: int,
    failure_kind: FailureKind,
    error: GitHubError,
    *,
    try_number: int | None = None,
) -> PublishResult:
    return await _fail(
        store,
        log,
        publication_id,
        failure_kind,
        error.last_error,
        http_status=error.http_status,
        try_number=try_number,
    )


async def _fail(
    store: PublicationStore,
    log: structlog.typing.FilteringBoundLogger,
    publication_id: int,
    failure_kind: FailureKind,
    last_error: str,
    *,
    http_status: int | None,
    try_number: int | None = None,
) -> PublishResult:
    await store.mark_failed(publication_id, failure_kind, last_error)
    extra = {} if try_number is None else {"try_number": try_number}
    log.warning(
        "publish.failed",
        failure_kind=failure_kind.value,
        http_status=http_status,
        last_error=last_error,
        **extra,
    )
    return PublishResult(PublishOutcome.FAILED, publication_id, last_error=last_error)
