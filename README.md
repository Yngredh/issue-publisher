# issue-publisher

Consome a fila `publish` e publica o comentário de triagem na issue de origem no GitHub.
É um worker sem HTTP: não expõe porta.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)
- Docker com Docker Compose (PostgreSQL e RabbitMQ locais e testes de integração)

## Configuração

Copie `.env.example` para `.env` e preencha os valores. São obrigatórios `DATABASE_URL`,
`RABBITMQ_URL` e `GITHUB_TOKEN` (segredo: nunca vai para o repositório nem para log). Os demais
têm padrão: `GITHUB_API_URL=https://api.github.com`, `GITHUB_BOT_LOGIN` vazio (a conta é lida de
`GET /user` na subida), `GITHUB_CONNECT_TIMEOUT_S=10`, `GITHUB_READ_TIMEOUT_S=15`,
`PUBLISH_MAX_TRIES=3` (de 1 a 3), `PUBLISH_RETRY_DELAYS_S=10,20` (pelo menos
`PUBLISH_MAX_TRIES - 1` valores), `PUBLISH_RETRY_AFTER_MAX_S=60` e `PUBLISH_PREFETCH=1`.
Para o compose local, as URLs são:

```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/issue_publisher
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
```

Nunca commite o `.env`.

## Subir a infraestrutura local

```bash
docker compose up -d      # PostgreSQL 16 (5432) e RabbitMQ (5672, painel em 15672)
docker compose down       # para os contêineres (use -v para apagar os dados)
```

O compose usa as mesmas portas do compose do issue-analyst-api; suba um de cada vez.

## Iniciar o worker

```bash
uv sync                                   # instala as dependências
uv run python -m issue_publisher.main     # inicia o worker; Ctrl+C (SIGINT) ou SIGTERM encerra
```

Com a imagem Docker:

```bash
docker build -t issue-publisher .
docker run --env-file .env issue-publisher
```

## Testes

```bash
uv run ruff check . && uv run pytest -q
```

## Documentação

- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-publisher.md](docs/plano-desenvolvimento-issue-publisher.md)
- Documentos do projeto: [../docs/doc-reference.md](../docs/doc-reference.md), que aponta para o índice
