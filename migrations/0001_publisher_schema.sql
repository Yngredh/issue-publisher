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
