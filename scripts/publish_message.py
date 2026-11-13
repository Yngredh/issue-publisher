"""Envia uma mensagem `{"proposalId", "revision"}` para a fila `publish` (ferramenta de dev).

Uso:
    uv run python scripts/publish_message.py <proposalId> <revision> [--rabbitmq-url URL]

A URL do RabbitMQ vem de `--rabbitmq-url`, ou de `RABBITMQ_URL` (variável de ambiente ou `.env`).
A fila `publish` é declarada durável e sem argumentos, como o `issue-triage` a declara (D6), para
que o fluxo local funcione sem o `issue-triage`; a mensagem é persistente e enviada com
confirmação do broker. Não lê banco nem GitHub: a proposta precisa existir na view
`triage.publishable_proposals` para ser publicada pelo worker.
"""

import argparse
import asyncio
import json
import sys

import aio_pika
from pydantic_settings import BaseSettings, SettingsConfigDict

PUBLISH_QUEUE = "publish"


class _BrokerSettings(BaseSettings):
    """Só a URL do RabbitMQ; não exige as demais variáveis do worker."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    RABBITMQ_URL: str | None = None


def build_body(proposal_id: int, revision: int) -> bytes:
    """Corpo da mensagem no contrato da fila `publish`."""
    return json.dumps({"proposalId": proposal_id, "revision": revision}).encode()


async def publish_message(rabbitmq_url: str, proposal_id: int, revision: int) -> None:
    """Publica a mensagem em `publish` e espera a confirmação do broker."""
    connection = await aio_pika.connect(rabbitmq_url)
    try:
        channel = await connection.channel(publisher_confirms=True)
        await channel.declare_queue(PUBLISH_QUEUE, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                build_body(proposal_id, revision),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=PUBLISH_QUEUE,
            mandatory=True,
        )
    finally:
        await connection.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Envia uma mensagem para a fila publish.")
    parser.add_argument("proposal_id", type=int, help="triage.proposals.id")
    parser.add_argument("revision", type=int, help="tentativa que gerou a proposta (>= 1)")
    parser.add_argument("--rabbitmq-url", help="URL AMQP; padrão: RABBITMQ_URL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    url = args.rabbitmq_url or _BrokerSettings().RABBITMQ_URL
    if not url:
        print("RABBITMQ_URL não definida (use --rabbitmq-url ou o .env).", file=sys.stderr)
        return 2
    asyncio.run(publish_message(url, args.proposal_id, args.revision))
    print(f"sent {build_body(args.proposal_id, args.revision).decode()} to {PUBLISH_QUEUE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
