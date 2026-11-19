from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração do processo, lida das variáveis de ambiente e de um `.env` opcional.

    `GITHUB_TOKEN` é segredo: fica em `SecretStr` e nunca aparece no `repr` nem em log.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "issue-publisher"
    DATABASE_URL: str
    RABBITMQ_URL: str
    GITHUB_TOKEN: SecretStr
    GITHUB_API_URL: str = "https://api.github.com"
    GITHUB_BOT_LOGIN: str = ""
    GITHUB_CONNECT_TIMEOUT_S: float = 10
    GITHUB_READ_TIMEOUT_S: float = 15
    PUBLISH_MAX_TRIES: int = Field(default=3, ge=1, le=3)
    PUBLISH_RETRY_DELAYS_S: Annotated[list[int], NoDecode] = [10, 20]
    PUBLISH_RETRY_AFTER_MAX_S: int = 60
    PUBLISH_PREFETCH: int = 1

    @field_validator("PUBLISH_RETRY_DELAYS_S", mode="before")
    @classmethod
    def _parse_delays(cls, value: object) -> object:
        """Aceita a lista separada por vírgulas da variável de ambiente (`10,20`)."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check_delays_cover_tries(self) -> Self:
        """Exige `max(1, PUBLISH_MAX_TRIES - 1)` atrasos (D14 P-a).

        Um para cada nova tentativa possível e, no mínimo, um para a falha temporária na busca
        do marcador (D12), que pode acontecer mesmo com `PUBLISH_MAX_TRIES = 1`.
        """
        required = max(1, self.PUBLISH_MAX_TRIES - 1)
        if len(self.PUBLISH_RETRY_DELAYS_S) < required:
            raise ValueError(
                f"PUBLISH_RETRY_DELAYS_S needs at least {required} values "
                f"for PUBLISH_MAX_TRIES={self.PUBLISH_MAX_TRIES}"
            )
        return self
