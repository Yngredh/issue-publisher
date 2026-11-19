"""Adaptador httpx da porta `GitHubIssuesPort` (RF23, RF24, RF25, RNF16).

Headers e timeouts de "Contratos > Chamadas ao GitHub" e versão da API da decisão D8. O token
só vai no header `Authorization`; nunca entra em log nem em mensagem de erro.
"""

from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx

from issue_publisher.config import Settings
from issue_publisher.domain.github import (
    MAX_ERROR_DETAIL_LENGTH,
    GitHubComment,
    classify_http_error,
    connection_error,
    timeout_error,
)

GITHUB_API_VERSION = "2022-11-28"  # D8
COMMENTS_PER_PAGE = 100


class HttpxGitHubIssues:
    """Cliente do GitHub com httpx assíncrono."""

    def __init__(
        self,
        *,
        token: str,
        api_url: str = "https://api.github.com",
        bot_login: str = "",
        connect_timeout_s: float = 10,
        read_timeout_s: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._bot_login = bot_login.strip()
        self._client = httpx.AsyncClient(
            base_url=api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            token=settings.GITHUB_TOKEN.get_secret_value(),
            api_url=settings.GITHUB_API_URL,
            bot_login=settings.GITHUB_BOT_LOGIN,
            connect_timeout_s=settings.GITHUB_CONNECT_TIMEOUT_S,
            read_timeout_s=settings.GITHUB_READ_TIMEOUT_S,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def identify_account(self) -> str:
        """`GITHUB_BOT_LOGIN`, se definido; senão o `login` de `GET /user`."""
        if self._bot_login:
            return self._bot_login
        response = await self._request("GET", "/user", expected=200)
        return str(response.json()["login"])

    async def find_comment_by_marker(
        self, repository: str, issue_number: int, marker: str, author: str
    ) -> GitHubComment | None:
        """Percorre as páginas (header `Link`) até achar o marcador de `author` ou acabar.

        Para no primeiro comentário que bate; o login é comparado sem diferenciar maiúsculas
        (`casefold`), porque o login do GitHub não diferencia (D13 e).
        """
        wanted = author.casefold()
        url: str | None = f"/repos/{repository}/issues/{issue_number}/comments"
        params: dict[str, Any] | None = {"per_page": COMMENTS_PER_PAGE}
        while url is not None:
            response = await self._request("GET", url, expected=200, params=params)
            for item in response.json():
                login = str((item.get("user") or {}).get("login") or "")
                if login.casefold() == wanted and marker in (item.get("body") or ""):
                    return _to_comment(item)
            url = response.links.get("next", {}).get("url")
            params = None  # a URL do `next` já traz a paginação
        return None

    async def create_comment(self, repository: str, issue_number: int, body: str) -> GitHubComment:
        """POST do comentário; 201 devolve id, `html_url` e `created_at`."""
        response = await self._request(
            "POST",
            f"/repos/{repository}/issues/{issue_number}/comments",
            expected=201,
            json={"body": body},
        )
        return _to_comment(response.json())

    async def _request(
        self, method: str, url: str, *, expected: int, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise timeout_error(type(exc).__name__) from None
        except httpx.TransportError as exc:
            raise connection_error(type(exc).__name__) from None
        if response.status_code != expected:
            raise classify_http_error(
                response.status_code, response.headers, self._safe_detail(response)
            )
        return response

    def _safe_detail(self, response: httpx.Response) -> str:
        """Detalhe do erro sem o token (mesmo se o GitHub o ecoar), truncado em 500 (D13 c).

        O token sai antes do corte, para que nenhum pedaço dele sobre no fim do texto.
        """
        detail = _detail(response)
        if self._token:
            detail = detail.replace(self._token, "***")
        return detail[:MAX_ERROR_DETAIL_LENGTH]


def _detail(response: httpx.Response) -> str:
    """`message` do corpo JSON do GitHub; sem ele, a frase do status HTTP."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return response.reason_phrase or "unexpected response"


def _to_comment(item: dict[str, Any]) -> GitHubComment:
    return GitHubComment(
        id=int(item["id"]),
        html_url=str(item["html_url"]),
        created_at=datetime.fromisoformat(item["created_at"]),
    )
