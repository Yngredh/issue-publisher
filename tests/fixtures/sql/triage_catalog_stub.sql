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
