"""Porta do GitHub (`GitHubIssuesPort`) e classificação das respostas (RF23, RF24, RF25).

O domínio não importa httpx: a porta descreve as três operações que o publisher faz no
GitHub, e a classificação trabalha só com status HTTP e headers.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol

MAX_ERROR_DETAIL_LENGTH = 500
"""Limite do detalhe de erro do GitHub (`message` ou frase do status), em caracteres (D13 c)."""


class ErrorClass(StrEnum):
    """Classe da falha, conforme "Contratos > Classes de resposta do GitHub"."""

    TEMPORARY = "temporary"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class GitHubComment:
    """Comentário confirmado pelo GitHub: o que vai para `publisher.publications`."""

    id: int
    html_url: str
    created_at: datetime


class GitHubError(Exception):
    """Falha classificada de uma chamada ao GitHub.

    `code` e `detail` formam o `last_error` (`<código>: <detalhe>`). `http_status` fica
    vazio em timeout e erro de conexão; `retry_after_s` vem do header `Retry-After`.
    """

    def __init__(
        self,
        error_class: ErrorClass,
        code: str,
        detail: str,
        *,
        http_status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        self.error_class = error_class
        self.code = code
        self.detail = detail
        self.http_status = http_status
        self.retry_after_s = retry_after_s
        super().__init__(self.last_error)

    @property
    def temporary(self) -> bool:
        return self.error_class is ErrorClass.TEMPORARY

    @property
    def last_error(self) -> str:
        """Texto gravado em `last_error`, por exemplo `http_403: Resource not accessible`."""
        return f"{self.code}: {self.detail}"


class GitHubIssuesPort(Protocol):
    """Operações do publisher no GitHub. Toda falha sai como `GitHubError`."""

    async def identify_account(self) -> str:
        """Login da conta do publisher (`GITHUB_BOT_LOGIN` ou `GET /user`)."""
        ...

    async def find_comment_by_marker(
        self, repository: str, issue_number: int, marker: str, author: str
    ) -> GitHubComment | None:
        """Procura em todas as páginas um comentário de `author` com `marker` no corpo."""
        ...

    async def create_comment(self, repository: str, issue_number: int, body: str) -> GitHubComment:
        """Cria o comentário e devolve a confirmação do GitHub (201 Created)."""
        ...


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Lê o `Retry-After` em segundos (número ou data HTTP); inválido ou ausente → `None`."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = (moment - (now or datetime.now(UTC))).total_seconds()
    return max(seconds, 0.0)


def classify_http_error(status: int, headers: Mapping[str, str], detail: str) -> GitHubError:
    """Classifica uma resposta HTTP sem sucesso (tabela de "Classes de resposta do GitHub").

    `headers` precisa ser insensível a maiúsculas (como os headers do httpx).
    5xx, 429 e 403 com `Retry-After` ou `x-ratelimit-remaining: 0` são temporárias; 401,
    os demais 403, 404, 410, 422 e os demais 4xx são permanentes.
    """
    retry_after_header = headers.get("retry-after")
    retry_after_s = parse_retry_after(retry_after_header)
    rate_limited = status == 429 or (
        status == 403
        and (
            retry_after_header is not None
            or headers.get("x-ratelimit-remaining", "").strip() == "0"
        )
    )
    temporary = status >= 500 or rate_limited
    return GitHubError(
        ErrorClass.TEMPORARY if temporary else ErrorClass.PERMANENT,
        f"http_{status}",
        detail,
        http_status=status,
        retry_after_s=retry_after_s,
    )


def timeout_error(detail: str) -> GitHubError:
    """Timeout de conexão ou de leitura: falha temporária (inclui POST sem resposta)."""
    return GitHubError(ErrorClass.TEMPORARY, "timeout", detail)


def connection_error(detail: str) -> GitHubError:
    """Erro de conexão ou de transporte: falha temporária."""
    return GitHubError(ErrorClass.TEMPORARY, "connection_error", detail)
