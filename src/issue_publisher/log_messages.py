"""Mensagens amigáveis dos logs do fluxo principal (RNF14).

Cada evento tem uma mensagem fixa em português, sem dados variáveis: o dado variável fica nos
campos do JSON. A mensagem nunca leva segredo, corpo do comentário nem a descrição da issue.
`add_message` é um processador do structlog: um `message=` passado no próprio log tem prioridade.
"""

from structlog.typing import EventDict, WrappedLogger

MESSAGES: dict[str, str] = {
    "publish.received": "Proposta recebida da fila de publicação",
    "publish.published": "Comentário criado na issue do GitHub",
    "publish.marker_found": "Comentário já existia na issue: nada novo foi publicado",
    "publish.skipped_stale": "Proposta de revisão antiga ignorada: já existe uma mais nova",
    "publish.discarded_invalid": "Mensagem de publicação descartada por ser inválida",
    "publish.failed": "Falha ao publicar o comentário no GitHub",
    "publish.retry_scheduled": "Falha temporária ao publicar: nova tentativa agendada",
    "publish.marker_search_failed": "Falha ao procurar o comentário já publicado na issue",
    "publish.processing_failed": "Erro ao processar a mensagem: ela volta para a fila",
    "publish.ack_failed": "Falha ao confirmar a mensagem depois de processada",
    "publish.requeue_failed": "Falha ao devolver a mensagem para a fila",
    "worker.identify_failed": "Falha ao identificar a conta do GitHub na subida",
    "worker.started": "Worker de publicação iniciado",
    "worker.stopped": "Worker de publicação encerrado",
}

MAIN_FLOW_EVENTS: tuple[str, ...] = tuple(MESSAGES)


def add_message(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Acrescenta `message` ao log; evento sem mensagem usa o próprio nome do evento."""
    event_dict.setdefault("message", MESSAGES.get(event_dict["event"], event_dict["event"]))
    return event_dict
