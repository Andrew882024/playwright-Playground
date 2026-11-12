"""Separate fail-closed credentials for read APIs and pipeline mutation routes."""
import hmac
import os
from fastapi import Header, HTTPException


def _require(authorization, setting):
    token = os.getenv(setting, '')
    if not 32 <= len(token) <= 512 or token.strip() != token:
        raise HTTPException(503, 'Service authentication is not configured.')
    supplied = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
    if len(supplied) != len(token) or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, 'Invalid service credential.')


def require_recovery_reader(authorization: str | None = Header(default=None)):
    _require(authorization, 'PLAYGROUND_RECOVERY_READ_TOKEN')


def require_sync_service(authorization: str | None = Header(default=None)):
    read_token, sync_token = os.getenv('PLAYGROUND_RECOVERY_READ_TOKEN'), os.getenv('PLAYGROUND_SYNC_TOKEN')
    if read_token and read_token == sync_token:
        raise HTTPException(503, 'Separate read and sync credentials are required.')
    _require(authorization, 'PLAYGROUND_SYNC_TOKEN')


# Keep existing authorized operations, but never return/log raw action exceptions
# through the HTTP exception boundary. This does not add any new action route.
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError


class SafeSyncRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, 'Invalid sync parameters.') from None
            except HTTPException as exc:
                if exc.status_code in (401,403,503):
                    raise HTTPException(exc.status_code, 'Sync service authorization/configuration unavailable.') from None
                raise HTTPException(503, 'Sync operation failed; inspect private server diagnostics.') from None
            except Exception:
                raise HTTPException(503, 'Sync operation failed; inspect private server diagnostics.') from None
        return safe_handler
