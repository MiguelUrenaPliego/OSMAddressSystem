"""Request-local identity; never populated from caller-supplied author fields."""
from contextvars import ContextVar

identity: ContextVar[dict | None] = ContextVar('address_identity', default=None)


def actor_id() -> str:
    actor=identity.get()
    return actor['id'] if actor else ''


def audit_identity(user: str) -> dict:
    actor=identity.get()
    if actor and actor['id']==user:
        return {'display_name':actor['display_name'],'username':actor['username'],'role':actor['role'],'source':'session'}
    return {'display_name':user or 'Unknown (legacy)','source':'legacy/system'}
