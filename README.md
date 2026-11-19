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
`max(1, PUBLISH_MAX_TRIES - 1)` valores), `PUBLISH_RETRY_AFTER_MAX_S=60` e `PUBLISH_PREFETCH=1`.
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

## Rodar localmente

```bash
uv sync                                   # instala as dependências
docker compose up -d                      # PostgreSQL e RabbitMQ locais
uv run python -m issue_publisher.migrate  # cria/atualiza o schema publisher (lê DATABASE_URL)
uv run python -m issue_publisher.main     # inicia o worker; Ctrl+C (SIGINT) ou SIGTERM encerra
```

O runner de migrações é idempotente e registra as versões em `publisher.schema_migrations`.
Ele só cria o schema `publisher`: a view `triage.publishable_proposals` (do `issue-triage`) e
`catalog.projects` (seed do ambiente) pertencem a outros serviços. Sem o `issue-triage`, para
experimentar o fluxo no banco local, aplique à mão o stub dos testes (nunca vai para
`migrations/`) e crie caso, tentativa, proposta e projeto do catálogo com `INSERT`:

```bash
docker compose exec -T postgres psql -U postgres -d issue_publisher < tests/fixtures/sql/triage_catalog_stub.sql
```

Na subida o worker identifica a conta do GitHub (`GET /user`, ou `GITHUB_BOT_LOGIN`), declara a
fila `publish.retry` e espera a fila `publish` existir (quem a declara é o `issue-triage`).

`GET /user` na subida: falha temporária (timeout, erro de conexão, 5xx, 429, 403 com limite de
taxa) tenta de novo com backoff de 1 s, dobrando até 30 s, e log de warning
`worker.identify_failed`, até dar certo ou vir o pedido de parada; falha permanente (401, 403 de
permissão, demais 4xx) gera o log de erro `worker.identify_failed` e encerra o processo com código
de saída 1, sem conectar ao broker. O log leva `failure_kind`, `http_status`, `last_error` e, na
falha temporária, `backoff_s`.

Se o broker cair com uma mensagem em curso, o processamento termina mesmo assim (o `POST` e o
commit são protegidos do cancelamento): só o ack falha, com o log de warning `publish.ack_failed`
e sem backoff. A mensagem volta para a fila quando o broker retorna, e a reentrega encontra a
publicação já `PUBLISHED`, sem chamar o GitHub de novo.

## Logs

JSON em stdout. Todo log de mensagem leva `event_id`, `repository`, `issue_number` e `revision`,
quando já conhecidos. Eventos:

| Evento | Quando |
|---|---|
| `publish.received` | mensagem com schema válido (leva `proposal_id` e `revision`); mensagem inválida gera só `publish.discarded_invalid` |
| `publish.discarded_invalid` | schema inválido, proposta inexistente ou `revision` diferente da view |
| `publish.skipped_stale` | `revision` menor que a tentativa atual do caso |
| `publish.marker_found` | comentário com o marcador já existe na issue e é adotado |
| `publish.marker_search_failed` | falha na busca do marcador (não consome tentativa) |
| `publish.try`, `publish.published`, `publish.failed` | tentativa de `POST`, sucesso e falha final |
| `publish.retry_scheduled` | cópia enviada para `publish.retry` com o atraso |
| `publish.processing_failed` | exceção no processamento (banco ou broker fora): espera o backoff e devolve a mensagem |
| `publish.requeue_failed` | o nack da devolução falhou porque o canal já caiu |
| `publish.ack_failed` | o ack falhou depois do commit (canal caído); sem backoff |
| `worker.identify_failed` | falha no `GET /user` da subida (warning se temporária, erro se permanente) |

Com a imagem Docker:

```bash
docker build -t issue-publisher .
docker run --env-file .env issue-publisher
```

## Enviar mensagens de teste

`scripts/publish_message.py` envia `{"proposalId": <id>, "revision": <n>}` para a fila `publish`
(ferramenta de desenvolvimento). A URL vem de `--rabbitmq-url` ou de `RABBITMQ_URL` (ambiente ou
`.env`). A fila `publish` é declarada durável e sem argumentos, como o `issue-triage` a declara,
para que o worker local comece a consumir sem ele.

```bash
uv run python scripts/publish_message.py 5 1
uv run python scripts/publish_message.py 5 1 --rabbitmq-url amqp://guest:guest@localhost:5672/
```

Atenção: com um `GITHUB_TOKEN` real, o worker publica de verdade na issue da proposta. Use só o
repositório de teste.

## Testes

```bash
uv run ruff check . && uv run pytest -q
```

O Docker precisa estar rodando: os testes de integração sobem PostgreSQL 16 e RabbitMQ com
testcontainers. **Nenhum teste chama o GitHub de verdade** e nenhum usa um token real.

| Testes | Real | Simulado |
|---|---|---|
| `tests/unit/test_config.py`, `test_retry_policy.py`, `test_preconditions.py`, `test_publish_message.py`, `test_github_classification.py` | regras do domínio e do `Settings` | leitor de propostas em memória |
| `tests/unit/test_github_adapter.py` | cliente httpx, headers, paginação, classificação | API do GitHub com respx |
| `tests/unit/test_consumer.py`, `test_worker.py`, `test_fake_github.py`, `test_publish_message_script.py` | ack, nova tentativa, sinais, script | broker, banco e GitHub com dublês |
| `tests/integration/test_publisher_schema.py`, `test_screen_message.py`, `test_claim_and_preconditions.py`, `test_publishing.py` | PostgreSQL (migrações e stub de `triage`/`catalog`) | GitHub pela porta falsa em memória |
| `tests/integration/test_logs.py` | PostgreSQL e cliente httpx | API do GitHub com respx |
| `tests/integration/test_consumer.py`, `test_end_to_end.py` | PostgreSQL e RabbitMQ (filas `publish` e `publish.retry`, queda do broker com `rabbitmqctl stop_app`) | GitHub pela porta falsa; banco fora do ar simulado no pool |

Os schemas `triage` e `catalog` existem nos testes só pelo stub
`tests/fixtures/sql/triage_catalog_stub.sql`. A demonstração contra o GitHub real (token
fine-grained da conta de teste, comentário no repositório de teste) é manual.

## Documentação

- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-publisher.md](docs/plano-desenvolvimento-issue-publisher.md)
- Documentos do projeto: [../docs/doc-reference.md](../docs/doc-reference.md), que aponta para o índice
