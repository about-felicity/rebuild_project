from contextvars import ContextVar

agent_project_id_ctx: ContextVar[str | None] = ContextVar("agent_project_id", default=None)