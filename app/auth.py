import os
import secrets
from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "admin")
SESSION_SECRET = os.getenv("SESSION_SECRET", "super-secret-default-key-change-me")


def verify_credentials(username: str, password: str) -> bool:
    username_ok = secrets.compare_digest(username.encode("utf-8"), APP_USERNAME.encode("utf-8"))
    password_ok = secrets.compare_digest(password.encode("utf-8"), APP_PASSWORD.encode("utf-8"))
    return username_ok and password_ok


def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated", False) is True


def require_auth(request: Request):
    if not is_authenticated(request):
        # If request is HTMX, trigger redirect header or 401
        if request.headers.get("HX-Request"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"HX-Redirect": "/login"}
            )
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"}
        )
