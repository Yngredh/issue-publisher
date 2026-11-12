# issue-publisher

Consome a fila `publish` e publica o comentário de triagem na issue de origem no GitHub.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)

## Comandos

```bash
uv sync                                 # instala as dependências
uv run pytest                           # roda os testes
uv run python -m issue_publisher.main   # inicia o worker
```

Copie `.env.example` para `.env` para sobrescrever as variáveis de ambiente.

## Documentação

- Plano de desenvolvimento: [docs/plano-desenvolvimento-issue-publisher.md](docs/plano-desenvolvimento-issue-publisher.md)
- Documentos do projeto: [../docs/doc-reference.md](../docs/doc-reference.md), que aponta para o índice
