"""Fixtures compartilhadas dos testes."""

import asyncio
import itertools
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from issue_publisher.migrate import apply_migrations

if sys.platform == "win32":
    # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Stub só de teste dos schemas `triage` e `catalog` (D11); nunca vai para `migrations/`.
TRIAGE_CATALOG_STUB = Path(__file__).parent / "fixtures" / "sql" / "triage_catalog_stub.sql"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """PostgreSQL 16 do testcontainers (imagem da D5), um por sessão de testes."""
    with PostgresContainer("postgres:16", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """URL do PostgreSQL da sessão, com as migrações e o stub de `triage`/`catalog` aplicados."""
    url = postgres_container.get_connection_url()
    apply_migrations(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(TRIAGE_CATALOG_STUB.read_text(encoding="utf-8"))
    return url


@pytest.fixture
def db_conn(database_url: str) -> Iterator[psycopg.Connection]:
    """Conexão dentro de uma transação desfeita ao fim do teste.

    Blocos `conn.transaction()` dentro do teste viram savepoints dessa transação externa.
    """
    with psycopg.connect(database_url) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


@dataclass(frozen=True)
class SeededProposal:
    """Dados criados pela fábrica: o que a view `triage.publishable_proposals` deve devolver."""

    proposal_id: int
    case_id: int
    attempt_id: int
    repository: str
    issue_number: int
    origin_event_id: str
    revision: int
    current_attempt: int
    case_status: str
    kind: str
    validation_status: str
    comment_body: str | None


def marker(origin_event_id: str, revision: int) -> str:
    """Marcador oculto do comentário (formato do ponto em aberto P2)."""
    return f"<!-- triage:{origin_event_id}:{revision} -->"


_DEFAULT_BODY = object()
_STATUS_REASON = {
    "READY_TO_PUBLISH": "proposal_validated",
    "ANALYZING": "initial",
    "NEEDS_REVIEW": "invalid_proposal",
    "MAX_RETRIES_REACHED": "manual_attempts_exhausted",
}
_issue_numbers = itertools.count(1)


def seed_proposal(
    conn: psycopg.Connection,
    *,
    repository: str = "acme/payments-api",
    issue_number: int | None = None,
    origin_event_id: str | None = None,
    revision: int = 1,
    current_attempt: int | None = None,
    case_status: str = "READY_TO_PUBLISH",
    status_reason: str | None = None,
    kind: str = "triage",
    validation_status: str = "VALID",
    comment_body: object = _DEFAULT_BODY,
    in_catalog: bool = True,
) -> SeededProposal:
    """Cria caso, tentativa, proposta e (opcionalmente) o projeto do catálogo.

    Por padrão a proposta está pronta para publicar: `VALID`, caso em `READY_TO_PUBLISH`,
    repositório no catálogo e corpo com exatamente um marcador do evento e da revisão.
    """
    if issue_number is None:
        issue_number = next(_issue_numbers)
    origin_event_id = origin_event_id or f"evt-{uuid.uuid4()}"
    if current_attempt is None:
        current_attempt = revision
    if status_reason is None:
        status_reason = _STATUS_REASON[case_status]
    if comment_body is _DEFAULT_BODY:
        comment_body = f"Triagem automática da issue.\n\n{marker(origin_event_id, revision)}"
    specialist = "bug" if kind == "triage" else None
    validation_error = None if validation_status == "VALID" else "invalid output"

    if in_catalog:
        conn.execute(
            "INSERT INTO catalog.projects (repository, name, responsible) VALUES (%s, %s, %s) "
            "ON CONFLICT (repository) DO NOTHING",
            (repository, repository.split("/")[1], "@maintainer"),
        )
    case_id = conn.execute(
        "INSERT INTO triage.issue_cases "
        "(repository, issue_number, origin_event_id, status, status_reason, current_attempt) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (repository, issue_number, origin_event_id, case_status, status_reason, current_attempt),
    ).fetchone()[0]
    attempt_id = conn.execute(
        "INSERT INTO triage.analysis_attempts (case_id, event_id, attempt, trigger, state) "
        "VALUES (%s, %s, %s, 'initial', 'DONE') RETURNING id",
        (case_id, origin_event_id, revision),
    ).fetchone()[0]
    proposal_id = conn.execute(
        "INSERT INTO triage.proposals "
        "(attempt_id, kind, specialist, validation_status, validation_error, comment_body) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (attempt_id, kind, specialist, validation_status, validation_error, comment_body),
    ).fetchone()[0]
    return SeededProposal(
        proposal_id=proposal_id,
        case_id=case_id,
        attempt_id=attempt_id,
        repository=repository,
        issue_number=issue_number,
        origin_event_id=origin_event_id,
        revision=revision,
        current_attempt=current_attempt,
        case_status=case_status,
        kind=kind,
        validation_status=validation_status,
        comment_body=comment_body,  # type: ignore[arg-type]
    )


@pytest.fixture
def make_proposal(db_conn: psycopg.Connection) -> Callable[..., SeededProposal]:
    """Fábrica de dados de teste ligada à conexão do teste (desfeita ao fim)."""

    def factory(**kwargs: object) -> SeededProposal:
        return seed_proposal(db_conn, **kwargs)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def make_committed_proposal(database_url: str) -> Iterator[Callable[..., SeededProposal]]:
    """Fábrica que grava os dados de teste de verdade (visíveis a outras conexões).

    Para testes que usam o pool ou conexões assíncronas próprias. Ao fim do teste apaga o
    que criou, inclusive linhas de `publisher` ligadas às propostas criadas.
    """
    created: list[SeededProposal] = []

    def factory(**kwargs: object) -> SeededProposal:
        with psycopg.connect(database_url) as conn:
            seeded = seed_proposal(conn, **kwargs)  # type: ignore[arg-type]
        created.append(seeded)
        return seeded

    yield factory

    with psycopg.connect(database_url) as conn:
        for seeded in created:
            conn.execute(
                "DELETE FROM publisher.publication_tries WHERE publication_id IN "
                "(SELECT id FROM publisher.publications WHERE proposal_id = %s)",
                (seeded.proposal_id,),
            )
            conn.execute(
                "DELETE FROM publisher.publications WHERE proposal_id = %s", (seeded.proposal_id,)
            )
            conn.execute("DELETE FROM triage.proposals WHERE id = %s", (seeded.proposal_id,))
            conn.execute(
                "DELETE FROM triage.analysis_attempts WHERE id = %s", (seeded.attempt_id,)
            )
            conn.execute("DELETE FROM triage.issue_cases WHERE id = %s", (seeded.case_id,))
