# issue-publisher

Último serviço do Assistente de manutenção de software: consome a fila `publish` e publica o comentário de
triagem na issue de origem no GitHub, sem duplicar. É um worker sem HTTP: não expõe porta. Faz parte de
um sistema de três serviços; a visão geral (arquitetura, como subir tudo, catálogo, exemplos e
demonstração) está no [README principal](https://github.com/Yngredh/issue-triage/blob/main/README.md), no repositório `issue-triage`.

## Papel no sistema

Recebe de [`issue-triage`](https://github.com/Yngredh/issue-triage), pela fila `publish`, a referência de
uma proposta pronta (`{"proposalId", "revision"}`) e a transforma em um comentário na issue, registrando
a confirmação do GitHub (id, URL e data). Quem enfileira é o `issue-triage` (pela outbox dele); a consulta
do resultado, no [`issue-analyst-api`](https://github.com/Yngredh/issue-analyst-api), lê o estado que este
serviço grava.

## Responsabilidades e o que não faz

- Valida a mensagem e lê a proposta pela view `triage.publishable_proposals` (repositório, issue, corpo
  e estado do caso vêm sempre da view, nunca da mensagem); mensagem inválida é descartada com log.
- Publica só `READY_TO_PUBLISH` de repositório que está no catálogo.
- Reivindica a publicação com `INSERT ... ON CONFLICT` em `(repository, issue_number, revision)` e
  procura o marcador oculto do comentário na issue antes de todo `POST`.
- Tenta publicar até 3 vezes, com espera progressiva (10 s e 20 s) pela fila `publish.retry`; token sem
  permissão ou issue inexistente é falha permanente.
- Grava o estado da publicação (`PENDING`, `PUBLISHED` ou `FAILED`), o id e a URL do comentário.
- **Não** chama o provedor de IA, não escreve em `triage` nem em `inbound` e não expõe HTTP.

## Interfaces

| Interface | Detalhe |
|---|---|
| Fila `publish` | Consome `{"proposalId", "revision"}`; quem a declara é o `issue-triage` |
| Fila `publish.retry` | Declarada por este serviço: fila de espera com expiração por mensagem e dead-letter de volta para `publish` |
| Schema `publisher` | Lê e escreve `publications` e `publication_tries` |
| `triage.publishable_proposals` e `catalog.projects` | Só leitura |
| API do GitHub | Identifica a conta (`GET /user`), procura o marcador nos comentários da issue e cria o comentário; token `GITHUB_TOKEN` |

## Métodos implementados aqui

Linhas da tabela de métodos do [README principal](https://github.com/Yngredh/issue-triage/blob/main/README.md) que cabem a este serviço:

| Método | Onde está neste repositório |
|---|---|
| Consumidor idempotente (deduplicação na publicação) | `src/issue_publisher/domain/publication.py` (`claim_and_check`, `build_marker`: reivindicação `INSERT ... ON CONFLICT DO NOTHING` + `FOR UPDATE` e marcador `<!-- triage:<eventId>:<revision> -->`); `src/issue_publisher/adapters/publications.py` (`PostgresPublicationStore`); `src/issue_publisher/adapters/github.py` (`find_comment_by_marker`); `src/issue_publisher/domain/publishing.py` (`publish_proposal`: busca do marcador antes de todo `POST`); `migrations/0001_publisher_schema.sql` (`UNIQUE (repository, issue_number, revision)`) |
| Nova tentativa com espera, sem DLQ (apoio, não conta como método) | `src/issue_publisher/domain/publishing.py` (`RetryPolicy`) e `src/issue_publisher/consumer.py` (`MessageHandler`, `_schedule_retry`): cópia da mensagem na fila `publish.retry`, que devolve a `publish` ao expirar |
| Portas e adaptadores (apoio, não conta como método) | `domain/github.py` (`GitHubIssuesPort`) com adaptador `adapters/github.py` (httpx) e simulado em `tests/fakes/github.py` |

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

## Executar isolado

Pré-requisitos: [uv](https://docs.astral.sh/uv/) (instala o Python 3.12, se necessário) e Docker com Docker Compose. O ambiente completo está no [README principal](https://github.com/Yngredh/issue-triage/blob/main/README.md).

### Subir a infraestrutura local

```bash
docker compose up -d      # PostgreSQL 16 (5432) e RabbitMQ (5672, painel em 15672)
docker compose down       # para os contêineres (use -v para apagar os dados)
```

O compose usa as mesmas portas do compose do issue-analyst-api; suba um de cada vez.

### Rodar localmente

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

### Imagem Docker

Com a imagem Docker:

```bash
docker build -t issue-publisher .
docker run --env-file .env issue-publisher
```

## Scripts e exemplos

### Enviar mensagens de teste

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

## Logs

JSON em stdout. Todo log de mensagem leva `event_id`, `repository`, `issue_number` e `revision`,
quando já conhecidos. Todo log também leva `module`, `func_name` (de onde o log saiu) e `message`
(texto fixo em português, definido em `src/issue_publisher/log_messages.py`). Exemplo:

```json
{"proposal_id": 7, "revision": 2, "event": "publish.received", "event_id": "12151e32-...", "repository": "Yaane-h/mechanical-hub-orchestrator", "issue_number": 1, "service": "issue-publisher", "level": "info", "timestamp": "2026-11-19T11:29:00Z", "module": "publish", "func_name": "screen_message", "message": "Proposta recebida da fila de publicação"}
```

Eventos:

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

## Limitações do serviço

- A fila `publish` não tem DLQ: o limite de 3 tentativas fica no banco (`publisher.publications`) e uma
  mensagem inválida (schema errado ou `proposalId` inexistente) é descartada com log.
- Se o serviço cair depois de comentar e antes de gravar `PUBLISHED`, a reentrega procura o marcador
  oculto e adota o comentário existente (`publish.marker_found`), sem criar outro.
- A view `triage.publishable_proposals` e `catalog.projects` pertencem a outros serviços e precisam
  existir; nos testes vêm de um stub.
- Com um `GITHUB_TOKEN` real, o worker publica de verdade: use só o repositório de teste.

## Documentação

- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-publisher.md](docs/plano-desenvolvimento-issue-publisher.md).
- Visão do sistema: [README principal](https://github.com/Yngredh/issue-triage/blob/main/README.md).
- Documentos de design: Issue Publisher Spec, Modelagem de dados, Requisitos e High Level Design, listados
  no README principal.
