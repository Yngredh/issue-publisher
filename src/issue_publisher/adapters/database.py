"""Adaptador de conexão com o PostgreSQL: pool assíncrono do psycopg (decisão D1)."""

from psycopg_pool import AsyncConnectionPool


def create_pool(database_url: str) -> AsyncConnectionPool:
    """Cria o pool sem abri-lo; quem abre e fecha é o worker."""
    return AsyncConnectionPool(conninfo=database_url, open=False)


async def open_pool(pool: AsyncConnectionPool) -> None:
    """Abre o pool sem esperar conexões prontas.

    O worker sobe mesmo com o banco fora do ar; o pool continua tentando conectar em segundo
    plano e a mensagem não é confirmada enquanto o banco não volta.
    """
    await pool.open(wait=False)


async def close_pool(pool: AsyncConnectionPool) -> None:
    """Fecha o pool e as conexões abertas."""
    await pool.close()
