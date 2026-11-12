# Plano de desenvolvimento — issue-publisher

Plano de implementação da **Issue Publisher Spec**, organizado em etapas para execução por agentes orquestrados. O `issue-publisher` consome a fila `publish`, lê a proposta validada pela view `triage.publishable_proposals` e transforma cada uma em exatamente um comentário na issue de origem, registrando a confirmação do GitHub (id e URL) no schema `publisher`. É o único serviço que escreve no GitHub; não expõe HTTP, não tem outbox e não escreve em `triage` nem em `inbound`.

Requisitos atendidos: RF22, RF23, RF24, RF25, RNF03, RNF07, RNF14, RNF16 e RNF18.

## Cabeçalho

**Stack.** Python 3.12 (uv), Pydantic v2, pydantic-settings, structlog, PostgreSQL (psycopg 3 async), RabbitMQ (aio-pika), httpx, pytest, pytest-asyncio, respx, testcontainers, ruff, Docker Compose. Detalhes em "Decisões técnicas da implementação".

**Comando de validação.** `uv run ruff check . && uv run pytest -q` (o Docker precisa estar rodando: os testes de integração sobem PostgreSQL e RabbitMQ com testcontainers).

**Branch.** `feature/publicacao-de-comentarios`. Uma etapa por commit, com a mensagem `etapa N - <título da etapa>`.

**Referências** (fonte da verdade; em caso de divergência, ver "Pontos em aberto"). Não existe `docs/referencias/` neste repositório, como no issue-analyst-api: a fonte da verdade é `../docs/doc-reference.md` (pasta `docs/` da raiz do projeto), que aponta para as páginas web de cada documento (registrado em 2026-11-12):
- Issue Publisher Spec — https://claude.ai/artifact/KpLq1aug1K7yFwaSfKQcvf
- Modelagem de dados — https://claude.ai/artifact/Atewr1HWqgsKSRHG8jmWwy (schemas `publisher` e `catalog`, view `triage.publishable_proposals`)
- Requisitos — https://claude.ai/artifact/BeRcRbCMDo6mE5RECR2xbm (RF22 a RF25)
- High Level Design — https://claude.ai/artifact/WymBNV3J3CvWvynqne1z4J (seções 3.2 Filas e 5 Tecnologias)
- Revisão Humana Spec — https://claude.ai/artifact/HZSnrGsfVhqf7CsS89ncUT (como a falha de publicação chega ao revisor)

As seções "Contratos" e "Modelo de dados" deste plano resumem o que cada etapa precisa, para que o agente não dependa de ler os documentos inteiros.

**Pré-requisitos (manuais, antes da Etapa 1)**
- [x] Criar o esqueleto do projeto com `uv init --package --name issue-publisher --python 3.12` (mesma base do issue-analyst-api: `config.py`, `logging.py`, `main.py`, ruff e pytest configurados) e commitar na `main` junto com este plano. Concluído em 2026-11-12, commit 3f6462f.
- [x] ~~Exportar os documentos de referência para `docs/referencias/`~~ Não se aplica (2026-11-12): as referências ficam em `../docs/doc-reference.md`, como no issue-analyst-api, e as seções "Contratos" e "Modelo de dados" deste plano trazem o que cada etapa precisa.
- [x] Validar as "Decisões técnicas da implementação" e os "Pontos em aberto" que bloqueiam etapas (indicados em cada etapa). D1 a D11 validadas pelo usuário em 2026-11-12; os pontos em aberto restantes (P2, P3, P6) não bloqueiam etapas.

## Como executar com agentes

- **Orquestrador** (sessão principal do Claude Code): não implementa nada. Atualiza a `main`, cria a branch, lê só os títulos `### Etapa N — ...` da seção "Atividades" e, para cada etapa com checkboxes pendentes, em ordem e uma por vez, chama o subagente `spec-step-implementer` com: caminho deste plano, número da etapa, branch e comando de validação. Depois de cada etapa CONCLUÍDA confere o commit (`git log -1 --oneline`) e faz `git push -u origin feature/publicacao-de-comentarios`. Em BLOQUEADA, para e mostra o relatório.
- **Subagente `spec-step-implementer`**: implementa só a etapa recebida, roda a validação até passar, marca os checkboxes concluídos (itens manuais ficam desmarcados), faz `git add` só dos arquivos que alterou e commita. Nunca faz push.
- **Regras para todas as etapas**
  - Não decidir nada que não esteja neste plano ou nos documentos de referência. Lacuna ou contradição → etapa BLOQUEADA com relatório.
  - Etapa com "Pré-condição" não atendida → BLOQUEADA.
  - Uma decisão (D) conta como validada só quando a linha "Status" de "Decisões técnicas da implementação" ou a própria linha da decisão diz "validada pelo usuário em <data>". Um ponto em aberto (P) conta como decidido quando a sua linha diz "Resolvido" ou "Decidido".
  - Testes de cada item junto da implementação; testes unitários em `tests/unit`, de integração em `tests/integration`.
  - Código, identificadores e logs em inglês; docstrings e textos para humanos em português.
  - Nunca chamar o GitHub de verdade nos testes: usar a porta falsa ou o respx. O token nunca aparece em log, em banco ou no repositório.
  - O publisher nunca escreve nos schemas `triage`, `inbound` ou `catalog` em código de produção.
  - Toda mensagem de commit cita os requisitos da etapa (por exemplo: `etapa 6 - Publicação com anti-duplicação (RF23, RF24, RNF03)`).

## Decisões técnicas da implementação

Escolhas de implementação que os documentos não fecham. **Status: D1 a D11 validadas pelo usuário em 2026-11-12 como estão; D12 e D13 validadas pelo usuário em 2026-11-12** (D6 já vinha decidida pela Issue Publisher Spec). D1 a D5 seguem o mesmo padrão do issue-analyst-api.

| # | Ponto | Escolha proposta |
|---|---|---|
| D1 | Driver do PostgreSQL | psycopg 3 assíncrono (`psycopg[binary,pool]`), com `AsyncConnectionPool` |
| D2 | Migrações | Arquivos SQL numerados em `migrations/`, aplicados por `python -m issue_publisher.migrate`, que registra a versão em `publisher.schema_migrations (version, applied_at)` |
| D3 | Cliente do RabbitMQ | aio-pika, com ack manual, publisher confirms e mensagens persistentes |
| D4 | Processo | Worker sem HTTP, iniciado por `python -m issue_publisher.main`, com encerramento limpo em SIGTERM/SIGINT (para de consumir, termina a mensagem em curso, fecha conexões) |
| D5 | Imagens locais | `postgres:16` e `rabbitmq:3-management` |
| D6 | Topologia das filas | Decidido (Issue Publisher Spec, "Topologia das filas"): o `issue-triage` declara a fila durável `publish`, sem argumentos, porque a alimenta pela outbox. O publisher declara na subida só a fila durável `publish.retry` com `x-dead-letter-exchange = ""` e `x-dead-letter-routing-key = publish`, e consome `publish` com declaração passiva, tentando de novo com backoff enquanto ela não existir. O atraso de cada cópia vai na expiração da própria mensagem (`expiration`) |
| D7 | Atraso da nova tentativa | `PUBLISH_RETRY_DELAYS_S[tries - 1]` depois da falha temporária de número `tries`; com `Retry-After`, usa o maior entre o atraso configurado e o `Retry-After`, limitado por `PUBLISH_RETRY_AFTER_MAX_S = 60`. Falha temporária na busca do marcador (sem `POST`): atraso da D12 |
| D8 | Versão da API do GitHub | header `X-GitHub-Api-Version: 2022-11-28` |
| D9 | Códigos de pré-condição em `last_error` | Além dos códigos da spec (`invalid_comment_body`, `marker_mismatch`, `repository_not_in_catalog`): `proposal_not_valid` (sem `VALID` ou sem `comment_body`) e `case_not_ready` (caso fora de `READY_TO_PUBLISH`) |
| D10 | Limite de `PUBLISH_MAX_TRIES` | Validado entre 1 e 3 no `Settings`, padrão 3: o limite é fixo em 3 na Issue Publisher Spec, e os `CHECK` de `publications.tries` e de `publication_tries.try_number` vão até 3 |
| D11 | Dados de `triage` e `catalog` nos testes | Script SQL só de teste em `tests/fixtures/sql/triage_catalog_stub.sql`, copiado da Modelagem de dados (tabelas `triage.issue_cases`, `triage.analysis_attempts`, `triage.proposals`, view `triage.publishable_proposals` e `catalog.projects`), aplicado pelas fixtures. Nunca vai para `migrations/`. Cópia local do DDL: seção "Modelo de dados > DDL de `triage` e `catalog` para o stub de teste (D11)" deste plano (registrado em 2026-11-12, porque os agentes não leem os documentos do claude.ai) |
| D12 | Falha na busca do marcador (passo 7), `published_at` adotado e `last_error` | **Validada pelo usuário em 2026-11-12.** A busca do marcador não consome tentativa (Issue Publisher Spec, "Contagem"): uma falha no `GET` dos comentários nunca grava `publication_tries` nem incrementa `tries`, e é classificada pela mesma tabela de "Classes de resposta do GitHub". 1) **Temporária** (timeout, erro de conexão, 5xx, 429, 403 com limite de taxa): atualiza `last_error`, mantém `PENDING`, faz commit e devolve o resultado "nova tentativa"; o atraso é `PUBLISH_RETRY_DELAYS_S[0]` quando `tries = 0` e `PUBLISH_RETRY_DELAYS_S[tries - 1]` nos demais casos, com a regra do `Retry-After` da D7. Não há limite de voltas: enquanto o GitHub não responder, a mensagem volta por `publish.retry`, com log de warning `publish.marker_search_failed` a cada volta (nada foi enviado ao GitHub, então não há risco de duplicar). 2) **Permanente** (401, 403 de permissão, 404, 410, 422 e demais 4xx, status fora de 4xx/5xx): `FAILED (permanent)` com `last_error`, sem `POST`. 3) Comentário adotado pela busca: `published_at` = `created_at` do comentário encontrado, como no 201. 4) `last_error` é atualizado a cada falha, temporária ou permanente, da busca ou do `POST`; o `FAILED` final guarda o erro da última tentativa |
| D13 | Escolhas feitas pelos subagentes nas Etapas 3 a 5 | **Validada pelo usuário em 2026-11-12: nove aceitas como estão e quatro corrigidas.** Aceitas: (a) falha sem resposta HTTP vira `timeout` ou `connection_error`, com o nome da exceção como detalhe; (b) status fora de 4xx e 5xx vira falha permanente `http_<status>`; (d) `Retry-After` aceita segundos ou data, e valor no passado vira 0; (f) erros no `GET /user` e na busca usam a mesma tabela de classes; (g) `proposalId` e `revision` só aceitam inteiro de fato; (h) JSON malformado vira `publish.discarded_invalid`; (i) só a primeira violação de pré-condição vai para `last_error`; (l) o limite de 65.536 conta caracteres Unicode; (m) o detalhe de `last_error` é uma frase livre em inglês. **Corrigidas:** (c) o detalhe do erro vem do `message` do JSON do GitHub ou, sem ele, da frase do status, **truncado em 500 caracteres**; (e) a busca para no primeiro comentário que bate, e o login é comparado **sem diferenciar maiúsculas** (`casefold`), porque o login do GitHub não diferencia; (j) corpo nulo → `proposal_not_valid`, corpo vazio **ou só com espaços** → `invalid_comment_body`; (k) "exatamente um marcador" vale para **qualquer** marcador no formato `<!-- triage:<eventId>:<revision> -->` no corpo: zero, mais de um ou um único com `eventId`/`revision` diferentes → `marker_mismatch` |

## Contratos

### Mensagem da fila `publish`
```json
{"proposalId": 5, "revision": 1}
```
Validada com Pydantic v2: `proposalId` inteiro, `revision` inteiro ≥ 1, sem campos extras. Repositório, número da issue, texto do comentário e estado do caso vêm sempre da view, nunca da mensagem. Entrega at-least-once: repetição é caso normal.

### Leitura da proposta
`SELECT ... FROM triage.publishable_proposals WHERE proposal_id = %s` devolve: `proposal_id`, `repository`, `issue_number`, `origin_event_id`, `revision`, `current_attempt`, `case_status`, `kind`, `validation_status`, `comment_body`. O repositório é conferido em `catalog.projects`.

### Processamento de uma mensagem (ordem obrigatória)
1. Validar o schema; inválido → ack, log `publish.discarded_invalid`, sem tocar no banco.
2. Ler a proposta pela view; inexistente ou `revision` diferente → ack e `publish.discarded_invalid`.
3. `revision < current_attempt` → ack, log `publish.skipped_stale`, sem linha.
4. Reivindicar: `INSERT ... ON CONFLICT (repository, issue_number, revision) DO NOTHING` com `state = PENDING`, `tries = 0`, e `SELECT ... FOR UPDATE` da linha numa transação que cobre a tentativa inteira.
5. `PUBLISHED` ou `FAILED` → commit e ack, sem chamar o GitHub.
6. Pré-condições (RF22): `validation_status = 'VALID'` e `comment_body` preenchido; `case_status = 'READY_TO_PUBLISH'`; repositório em `catalog.projects`; corpo válido (não vazio, até 65.536 caracteres, exatamente um marcador `<!-- triage:<origin_event_id>:<revision> -->`). Violação → `FAILED`, `failure_kind = 'permanent'`, `last_error` com o código, sem chamar o GitHub.
7. Procurar o marcador na issue (em toda tentativa): achou, com autor = conta do publisher (login comparado sem diferenciar maiúsculas, D13) → adota id, URL e `published_at` = `created_at` do comentário, `PUBLISHED`. Falha na busca: D12 (não consome tentativa; temporária → nova tentativa, permanente → `FAILED (permanent)`).
8. Criar o comentário: POST, grava `publication_tries` (`try_number`, `http_status` ou `error`) e incrementa `tries`.
9. Aplicar o resultado (tabela de classes abaixo).
10. Commit da transação e, só depois, ack. Nova tentativa: commit, publica a cópia em `publish.retry` com confirmação do broker e só então ack da original.

Banco ou broker indisponível: não confirma a mensagem e não gasta tentativa.

### Classes de resposta do GitHub
| Situação | Classe | Resultado |
|---|---|---|
| 201 Created | Sucesso | `PUBLISHED` com `github_comment_id` (`id`), `github_comment_url` (`html_url`), `published_at` (`created_at`) |
| Timeout, erro de conexão, 5xx | Temporária | Nova tentativa; na última, `FAILED (retries_exhausted)` |
| 429, ou 403 com `Retry-After` ou `x-ratelimit-remaining: 0` | Temporária | Nova tentativa respeitando o `Retry-After` (D7); na última, `FAILED (retries_exhausted)` |
| 401, 403 de permissão (inclui issue travada) | Permanente | `FAILED (permanent)` |
| 404, 410 | Permanente | `FAILED (permanent)` |
| 422 e demais 4xx | Permanente | `FAILED (permanent)` |

`last_error` no formato `<código>: <detalhe>`, por exemplo `http_403: Resource not accessible`, com o detalhe truncado em 500 caracteres (D13) e atualizado a cada falha, da busca ou do `POST` (D12). A mesma tabela vale para o `GET` da busca do marcador, que nunca consome tentativa (D12). POST com timeout conta como temporária, e a próxima tentativa começa pela busca do marcador.

### Chamadas ao GitHub (porta `GitHubIssuesPort`)
| Operação | Chamada |
|---|---|
| Identificar a conta (subida) | `GET /user`, a menos que `GITHUB_BOT_LOGIN` esteja definido |
| Procurar o marcador | `GET /repos/{owner}/{repo}/issues/{number}/comments?per_page=100`, seguindo todas as páginas; só conta comentário com `user.login` = conta do publisher e o marcador exato no corpo |
| Criar o comentário | `POST /repos/{owner}/{repo}/issues/{number}/comments` com `{"body": ...}` |

Headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version` (D8). Timeouts de conexão e de leitura configuráveis.

### Variáveis de ambiente
| Variável | Padrão |
|---|---|
| `LOG_LEVEL`, `SERVICE_NAME` | `INFO`, `issue-publisher` |
| `DATABASE_URL`, `RABBITMQ_URL` | sem padrão |
| `GITHUB_TOKEN` | sem padrão, segredo |
| `GITHUB_API_URL` | `https://api.github.com` |
| `GITHUB_BOT_LOGIN` | vazio = lido de `GET /user` na subida |
| `GITHUB_CONNECT_TIMEOUT_S`, `GITHUB_READ_TIMEOUT_S` | `10`, `15` |
| `PUBLISH_MAX_TRIES` | `3` (1 a 3, D10) |
| `PUBLISH_RETRY_DELAYS_S` | `10,20` |
| `PUBLISH_RETRY_AFTER_MAX_S` | `60` (D7) |
| `PUBLISH_PREFETCH` | `1` |

### Logs
Todo log leva `event_id` (o `origin_event_id`), `repository`, `issue_number` e `revision`. Eventos: `publish.received`, `publish.skipped_stale`, `publish.discarded_invalid`, `publish.marker_found`, `publish.try`, `publish.published`, `publish.retry_scheduled`, `publish.failed`.

## Modelo de dados (schema `publisher`, conforme a Modelagem de dados)

```sql
CREATE SCHEMA IF NOT EXISTS publisher;

CREATE TABLE publisher.publications (
  id                  bigserial PRIMARY KEY,
  repository          text NOT NULL,
  issue_number        int  NOT NULL,
  revision            int  NOT NULL,              -- attempt que gerou a proposta
  proposal_id         bigint NOT NULL,            -- triage.proposals.id
  origin_event_id     text NOT NULL,              -- vai no marcador oculto do comentário
  kind                text NOT NULL CHECK (kind IN ('triage', 'missing_info')),
  state               text NOT NULL CHECK (state IN ('PENDING', 'PUBLISHED', 'FAILED')),
  tries               smallint NOT NULL DEFAULT 0 CHECK (tries BETWEEN 0 AND 3),
  failure_kind        text CHECK (failure_kind IN ('retries_exhausted', 'permanent')),
  last_error          text,
  github_comment_id   bigint,
  github_comment_url  text,
  published_at        timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository, issue_number, revision),    -- chave de deduplicação da publicação
  CHECK (state <> 'PUBLISHED' OR (github_comment_id IS NOT NULL AND github_comment_url IS NOT NULL)),
  CHECK (state <> 'FAILED' OR failure_kind IS NOT NULL)
);

CREATE TABLE publisher.publication_tries (
  id              bigserial PRIMARY KEY,
  publication_id  bigint NOT NULL REFERENCES publisher.publications (id),
  try_number      smallint NOT NULL CHECK (try_number BETWEEN 1 AND 3),
  http_status     int,
  error           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (publication_id, try_number)
);
```

Lidos, mas de outros donos (só existem nos testes pelo script de D11): a view `triage.publishable_proposals` (dona: `issue-triage`) e a tabela `catalog.projects` (seed do ambiente). As linhas de `publications` nunca são apagadas.

### DDL de `triage` e `catalog` para o stub de teste (D11)

Cópia literal da Modelagem de dados (revisão 139, de 2026-11-12), restrita ao que a view `triage.publishable_proposals` e `catalog.projects` precisam: as três tabelas de `triage`, a view e `catalog.projects`. Ficaram de fora, de propósito, `triage.route_decisions`, `triage.publish_outbox`, `catalog.triage_guidelines`, o índice `analysis_attempts_one_per_edit` e os papéis e `GRANT` (P6). Este é o conteúdo de `tests/fixtures/sql/triage_catalog_stub.sql`; os schemas `triage` e `catalog` pertencem a outros serviços e o stub nunca vai para `migrations/`. Se a Modelagem de dados mudar essas tabelas, este bloco é atualizado aqui primeiro.

```sql
CREATE SCHEMA IF NOT EXISTS triage;
CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE triage.issue_cases (
  id               bigserial PRIMARY KEY,
  repository       text NOT NULL,
  issue_number     int  NOT NULL,
  origin_event_id  text NOT NULL UNIQUE,           -- primeiro issue.created da issue
  status           text NOT NULL CHECK (status IN (
                     'ANALYZING', 'NEEDS_REVIEW', 'MAX_RETRIES_REACHED',
                     'READY_TO_PUBLISH', 'CANCELLED', 'FINISHED')),
  status_reason    text NOT NULL,                  -- motivo do status (combinação no CHECK abaixo)
  category         text CHECK (category IN ('bug', 'melhoria', 'duvida', 'indeterminado')),
  current_attempt  int NOT NULL DEFAULT 1 CHECK (current_attempt >= 1),
  manual_attempts  int NOT NULL DEFAULT 0 CHECK (manual_attempts >= 0),  -- limite: MAX_MANUAL_ATTEMPTS
  closed_by_review_id  bigint,                 -- inbound.issue_review_history.id
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository, issue_number),               -- um caso por issue
  CHECK (
    (status = 'ANALYZING'              AND status_reason IN ('initial', 'author_edit', 'enrich', 'reprocess'))
    OR (status = 'READY_TO_PUBLISH'    AND status_reason = 'empty_description')
    OR (status = 'READY_TO_PUBLISH'    AND status_reason = 'proposal_validated')
    OR (status = 'NEEDS_REVIEW'        AND status_reason IN ('unknown_repo', 'insufficient_content',
                                                           'invalid_proposal', 'technical_failure'))
    OR (status = 'MAX_RETRIES_REACHED' AND status_reason = 'manual_attempts_exhausted')
    OR (status IN ('CANCELLED', 'FINISHED') AND status_reason = 'closed_by_reviewer')
  ),
  CHECK (status NOT IN ('CANCELLED', 'FINISHED') OR closed_by_review_id IS NOT NULL)
);

CREATE TABLE triage.analysis_attempts (
  id           bigserial PRIMARY KEY,
  case_id      bigint NOT NULL REFERENCES triage.issue_cases (id),
  event_id     text NOT NULL,                      -- origin_event_id do caso
  attempt      int  NOT NULL CHECK (attempt >= 1),
  trigger      text NOT NULL CHECK (trigger IN ('initial', 'author_edit', 'enrich', 'reprocess')),
  trigger_ref  text,                               -- eventId do issues.edited ou id da resolução
  state        text NOT NULL CHECK (state IN ('IN_PROGRESS', 'DONE')),
  outcome      text,                               -- status do caso ao terminar a tentativa
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  UNIQUE (event_id, attempt)                       -- chave de deduplicação da fila analysis
);

CREATE TABLE triage.proposals (
  id                 bigserial PRIMARY KEY,
  attempt_id         bigint NOT NULL UNIQUE REFERENCES triage.analysis_attempts (id),
  kind               text NOT NULL CHECK (kind IN ('triage', 'missing_info')),
  specialist         text CHECK (specialist IN ('bug', 'melhoria', 'dominio')),
  category           text CHECK (category IN ('bug', 'melhoria', 'duvida')),
  summary            text,
  analysis_result    text,
  evidence           jsonb NOT NULL DEFAULT '[]',  -- trechos do evento ou do catálogo
  tool_calls         jsonb NOT NULL DEFAULT '[]',  -- ferramenta, argumentos, duração (RNF11)
  raw_output         jsonb,                        -- saída bruta, guardada mesmo se reprovada (RF31)
  validation_status  text NOT NULL CHECK (validation_status IN ('VALID', 'INVALID')),
  validation_error   text,                         -- motivo da reprovação
  comment_body       text,                         -- texto final do template, com marcador oculto
  prompt_version     text,
  model              text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (kind = 'missing_info' OR specialist IS NOT NULL),
  CHECK (validation_status = 'VALID' OR validation_error IS NOT NULL),
  CHECK (validation_status = 'INVALID' OR comment_body IS NOT NULL)
);

CREATE VIEW triage.publishable_proposals AS
SELECT
  pr.id             AS proposal_id,
  c.repository,
  c.issue_number,
  c.origin_event_id,                       -- vai no marcador oculto
  a.attempt         AS revision,           -- attempt que gerou a proposta
  c.current_attempt,                       -- para descartar mensagem obsoleta
  c.status          AS case_status,        -- precisa estar em READY_TO_PUBLISH
  pr.kind,
  pr.validation_status,
  pr.comment_body
FROM triage.proposals pr
JOIN triage.analysis_attempts a ON a.id = pr.attempt_id
JOIN triage.issue_cases c ON c.id = a.case_id;

CREATE TABLE catalog.projects (
  repository   text PRIMARY KEY CHECK (repository ~ '^[^/\s]+/[^/\s]+$'),
  name         text NOT NULL,
  responsible  text NOT NULL,              -- citado no comentário (RF19)
  description  text NOT NULL DEFAULT ''
);
```

## Atividades

### Etapa 1 — Fundação do worker
Requisitos: RNF14, RNF18. Depende de: pré-requisitos. Pré-condição: decisões D1 a D5 e D10 validadas.
- [x] Adicionar dependências com `uv add`: `psycopg[binary,pool]`, `aio-pika`, `httpx`; e com `uv add --dev`: `testcontainers[postgres,rabbitmq]`, `respx`.
- [x] Completar `Settings` com as variáveis de "Contratos > Variáveis de ambiente" (`GITHUB_TOKEN` como `SecretStr` obrigatório; `PUBLISH_RETRY_DELAYS_S` lido como lista de inteiros a partir de `10,20`; `PUBLISH_MAX_TRIES` entre 1 e 3; a lista de atrasos precisa ter pelo menos `PUBLISH_MAX_TRIES - 1` valores) e ajustar os testes existentes.
- [x] Fazer `main.py` iniciar o worker com encerramento limpo em SIGTERM/SIGINT (D4); o consumo de verdade entra na Etapa 7.
- [x] Atualizar o `.env.example` (só nomes) e criar `docker-compose.yml` local com PostgreSQL e RabbitMQ para desenvolvimento.
- [x] Criar `Dockerfile` do serviço baseado em uv.
- [x] Testes: `Settings` falha sem `GITHUB_TOKEN`, com `PUBLISH_MAX_TRIES` fora de 1 a 3 e com lista de atrasos curta demais; o token não aparece no `repr` nem em log.
- [x] Atualizar o README com como subir o compose e iniciar o worker.

### Etapa 2 — Schema publisher, migrações e dados de teste
Requisitos: RNF07. Depende de: Etapa 1. Pré-condição: decisões D2 e D11 validadas.
- [x] Criar `migrations/0001_publisher_schema.sql` com o DDL da seção "Modelo de dados".
- [x] Criar o runner `python -m issue_publisher.migrate` (D2), idempotente.
- [x] Criar o adaptador de conexão em `adapters/` (pool assíncrono do psycopg).
- [x] Criar `tests/fixtures/sql/triage_catalog_stub.sql` (D11) com o conteúdo exato da seção "Modelo de dados > DDL de `triage` e `catalog` para o stub de teste (D11)", sem acrescentar nem remover nada.
- [x] Criar fixtures em `tests/conftest.py`: PostgreSQL do testcontainers por sessão com as migrações e o stub aplicados, e uma fábrica de dados de teste que cria caso, tentativa, proposta e projeto do catálogo.
- [x] Testes de integração: a chave única `(repository, issue_number, revision)`; `PUBLISHED` sem id ou URL é recusado; `FAILED` sem `failure_kind` é recusado; `tries` e `try_number` fora de 0 a 3 são recusados; o runner é idempotente; a view do stub devolve as colunas de "Contratos > Leitura da proposta".

### Etapa 3 — Porta do GitHub e cliente HTTP
Requisitos: RF23, RF24, RF25, RNF16. Depende de: Etapa 1. Pré-condição: decisões D7 e D8 validadas.
- [x] Definir a porta `GitHubIssuesPort` em `domain/` com: identificar a conta, procurar comentário por marcador e autor, e criar comentário.
- [x] Implementar o adaptador httpx: headers e timeouts de "Contratos", paginação até a última página (header `Link`), e na criação devolver id, `html_url` e `created_at` em sucesso, ou um erro classificado.
- [x] Implementar a classificação de "Contratos > Classes de resposta", incluindo a leitura de `Retry-After` e `x-ratelimit-remaining`, e o formato de `last_error`.
- [x] Implementar uma porta falsa em memória para os testes dos próximos passos (comentários por issue, respostas programáveis por chamada, contagem de POSTs).
- [x] Testes com respx: paginação com marcador na última página; marcador de outro autor ignorado; cada linha da tabela de classes; timeout e erro de conexão como temporários; `Retry-After` lido; o token não aparece em log nem em mensagem de erro.

### Etapa 4 — Mensagem e leitura da proposta
Requisitos: RF22, RNF07. Depende de: Etapa 2.
- [x] Modelo Pydantic da mensagem `publish` (`extra = "forbid"`, `revision >= 1`).
- [x] Repositório de leitura da view `triage.publishable_proposals` por `proposal_id` e da presença do repositório em `catalog.projects`.
- [x] Passos 1 a 3 do processamento como lógica testável sem broker: schema inválido, proposta inexistente ou `revision` diferente → descartar; `revision < current_attempt` → obsoleta.
- [x] Testes unitários do modelo e testes de integração dos passos 1 a 3 com o stub, confirmando que nenhum desses casos cria linha em `publications`.

### Etapa 5 — Reivindicação e pré-condições
Requisitos: RF22, RNF03. Depende de: Etapa 4. Pré-condição: decisão D9 validada.
- [x] Reivindicação (passo 4): `INSERT ... ON CONFLICT (repository, issue_number, revision) DO NOTHING` com `state = PENDING`, `tries = 0` e as colunas de reivindicação da spec (`proposal_id`, `origin_event_id`, `kind`), seguida de `SELECT ... FOR UPDATE` na mesma transação.
- [x] Estado final já registrado (passo 5): encerrar sem chamar o GitHub.
- [x] Pré-condições (passo 6) e regras do corpo, encerrando em `FAILED (permanent)` com o código em `last_error` (spec e D9), sem chamar o GitHub.
- [x] Testes de integração com a porta falsa: repositório fora do catálogo (cenário 11); corpo sem marcador ou com marcador de outro evento (cenário 14); corpo vazio ou acima de 65.536 caracteres; proposta `INVALID`; caso fora de `READY_TO_PUBLISH`; linha já `PUBLISHED` ou `FAILED` não chama a porta; nenhum desses casos faz POST.

### Etapa 6 — Publicação com anti-duplicação
Requisitos: RF23, RF24, RNF03. Depende de: Etapas 3 e 5. Pré-condição: decisões D12 e D13 validadas.
- [x] Correção da D13 na Etapa 3 (c): truncar em 500 caracteres o detalhe de erro vindo do `message` do GitHub ou da frase do status; teste com mensagem maior que 500.
- [x] Correção da D13 na Etapa 3 (e): comparar `user.login` com a conta do publisher sem diferenciar maiúsculas (`casefold`); teste com `GITHUB_BOT_LOGIN` em outra caixa.
- [x] Correção da D13 na Etapa 5 (j): corpo só com espaços → `invalid_comment_body`; ajustar o teste que hoje espera `marker_mismatch`.
- [x] Correção da D13 na Etapa 5 (k): contar todos os marcadores `<!-- triage:<eventId>:<revision> -->` do corpo; exatamente um e igual ao do caso e da revisão, senão `marker_mismatch`; testes com marcador de outro evento junto do correto e com dois marcadores iguais.
- [x] Passo 7: busca do marcador antes de todo POST; comentário encontrado com autor = conta do publisher → adota id, URL e `published_at` = `created_at` do comentário (D12), grava `PUBLISHED`, log `publish.marker_found`.
- [x] Passo 7, falha na busca (D12): não grava `publication_tries` nem incrementa `tries`; temporária → atualiza `last_error`, mantém `PENDING`, commit, resultado "nova tentativa" com o atraso da D12 e log de warning `publish.marker_search_failed`; permanente → `FAILED (permanent)` sem `POST`.
- [x] `last_error` atualizado a cada falha temporária do `POST` (D12).
- [x] Passo 8: POST, linha em `publication_tries` e `tries` incrementado na mesma transação.
- [x] Passo 9 para sucesso e falha permanente: 201 grava `PUBLISHED` com id, URL e `published_at`; permanente grava `FAILED (permanent)`. A falha temporária devolve o resultado "nova tentativa" ou, na última tentativa, `FAILED (retries_exhausted)`; o envio a `publish.retry` fica para a Etapa 7.
- [x] Testes de integração com a porta falsa: proposta pronta publica um comentário (cenário 1); descrição vazia publica o comentário-template (cenário 15); marcador colado por outro autor é ignorado (cenário 12); queda depois do POST e antes do commit, com reprocessamento que adota o comentário e totaliza um só (cenário 5); 403 de permissão vira `FAILED (permanent)` na 1ª tentativa (cenário 9); busca com timeout não muda `tries` nem `publication_tries` e devolve "nova tentativa" com atraso de `PUBLISH_RETRY_DELAYS_S[0]`; busca com 404 vira `FAILED (permanent)` sem `POST`; comentário adotado grava `published_at` do `created_at`; o `FAILED (retries_exhausted)` guarda o `last_error` da última falha.

### Etapa 7 — Consumidor, retentativas e topologia das filas
Requisitos: RF25, RNF03. Depende de: Etapa 6. Pré-condição: decisões D3 e D7 validadas.
- [ ] Declarar `publish.retry` na subida e consumir `publish` com declaração passiva, tentando de novo com backoff enquanto ela não existir (D6). Nos testes, a fixture declara `publish` como o `issue-triage` fará.
- [ ] Consumir `publish` com `prefetch = PUBLISH_PREFETCH` e ack manual, ligando os passos 1 a 10; ack só depois do commit.
- [ ] Nova tentativa: depois do commit, publicar a cópia da mensagem em `publish.retry` com a expiração de D7 (ou da D12, quando a falha foi na busca do marcador) e confirmação do broker, e só então confirmar a original (log `publish.retry_scheduled`).
- [ ] Banco ou broker indisponível: não confirmar a mensagem (volta para a fila), sem gastar tentativa; reconectar com backoff.
- [ ] Testes de integração com RabbitMQ e PostgreSQL do testcontainers e a porta falsa, usando atrasos curtos nos testes (por exemplo `PUBLISH_RETRY_DELAYS_S=1,2`) e conferindo os valores de expiração: mesma mensagem entregue duas vezes faz um só POST (cenário 2); dois consumidores com a mesma mensagem fazem um só POST (cenário 4); 503, 503 e 201 terminam em `PUBLISHED` com `tries = 3` (cenário 7); 5xx três vezes termina em `FAILED (retries_exhausted)` (cenário 8); 429 com `Retry-After` respeita o prazo (cenário 10); `revision` obsoleta é confirmada sem efeito (cenário 13); schema inválido e `proposalId` inexistente são descartados (cenário 16); banco parado não confirma a mensagem e ela é processada quando o banco volta; busca do marcador com 503 e depois 201 no POST termina em `PUBLISHED` com `tries = 1` (D12).

### Etapa 8 — Observabilidade
Requisitos: RNF14. Depende de: Etapa 7.
- [ ] Os oito eventos de log de "Contratos > Logs", todos com `event_id`, `repository`, `issue_number` e `revision` (quando já conhecidos), e com `try_number`, `http_status`, atraso, `failure_kind` e `last_error` onde a spec indica.
- [ ] `event_id` ligado ao contexto do structlog durante o processamento de cada mensagem.
- [ ] Nenhum log contém o token nem o corpo do comentário.
- [ ] Testes com `structlog.testing.capture_logs` cobrindo cada evento e a ausência do token.

### Etapa 9 — Resiliência, ferramentas locais e demonstração
Requisitos: RF22 a RF25, RNF16. Depende de: Etapa 8.
- [ ] Criar `scripts/publish_message.py`, que envia uma mensagem `{"proposalId", "revision"}` para a fila `publish` (ferramenta de desenvolvimento).
- [ ] Testes de ponta a ponta do worker (PostgreSQL e RabbitMQ reais, porta falsa): mensagens acumuladas com o worker parado são publicadas quando ele volta (cenário 6, parte do publisher); queda do broker durante o consumo não perde a mensagem.
- [ ] README: como rodar localmente, migrar, enviar mensagens de teste, rodar os testes e o que é real ou simulado em cada teste.
- [ ] (manual) Criar o token fine-grained da conta de teste com permissão de escrita em issues só no repositório de teste e configurá-lo no `.env`.
- [ ] (manual) Publicar um comentário real no repositório de teste e conferir id e URL gravados (cenário 1 real).
- [ ] (manual) Depois que o `issue-triage` existir, executar o fluxo completo: webhook reenviado não gera comentário novo (cenário 3) e publisher parado com a análise funcionando (cenário 6).

## Pontos em aberto

Divergências entre documentos e lacunas. Cada decisão tomada aqui deve voltar para a Issue Publisher Spec, a Modelagem de dados e o High Level Design.

| # | Ponto | Situação | Bloqueia |
|---|---|---|---|
| P1 | Nomes no schema `inbound` | Resolvido: a Issue Publisher Spec passou a usar `inbound.issue_presentation` e `inbound.issue_events`, como a Modelagem de dados | — |
| P2 | Formato do marcador oculto | A spec assume `<!-- triage:<eventId>:<revision> -->` e pede que o formato seja registrado na spec da triagem. Este plano usa esse formato | — (registrar na spec da triagem) |
| P3 | Origem do catálogo no banco | O High Level Design diz que o catálogo é o `catalog.yaml` lido do disco pelo `issue-triage`; a Modelagem de dados e a Issue Publisher Spec leem `catalog.projects` no banco, carregado por seed. Falta definir quem gera o seed a partir do `catalog.yaml` | Não bloqueia os testes (stub de D11); bloqueia a execução integrada |
| P4 | Quem declara a topologia de `publish` | Resolvido: o `issue-triage` declara `publish`; o publisher declara só `publish.retry` (D6). Registrado na Issue Publisher Spec e no High Level Design | — |
| P5 | Limite de tentativas acima de 3 | Resolvido: o limite fica em 3. A Issue Publisher Spec não prevê mais o quarto atraso, e `PUBLISH_RETRY_DELAYS_S` passou a `10,20` | — |
| P6 | Papéis, permissões e compose integrado | O papel `publisher_svc` e os `GRANT` atravessam serviços; falta decidir onde ficam e onde mora o compose dos três serviços (mesmo ponto do issue-analyst-api) | Não bloqueia: este plano usa o compose local e `DATABASE_URL` |

## Fora do escopo

Geração do texto do comentário e do marcador (`issue-triage`), view `triage.publishable_proposals` e tabelas de `triage`, seed do catálogo, API de consulta e view de apresentação (`issue-analyst-api`), edição ou remoção de comentários publicados, fechamento de issue e pull requests.

## Limitações assumidas

Busca do marcador e POST não são atômicos no GitHub (outro cliente publicando o mesmo marcador não é detectado); listagem atrasada do GitHub pode, numa reentrega imediata, criar um segundo comentário; comentário apagado à mão numa publicação `PENDING` é recriado; o lock da linha mantém uma conexão aberta durante a chamada HTTP; reprocessar uma falha de publicação refaz a análise inteira; mensagem inválida é descartada só com log, sem DLQ; com `publish.retry` por expiração, um atraso longo pode segurar um curto que esteja atrás dele.
