"""Standalone FastAPI bridge between Lovable and canonical Trading Desk state."""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import api_repository
from api_auth import TokenVerificationError, VerifiedIdentity, verify_access_token
from public_contract import error_payload


def _origins() -> list[str]:
    configured = [value.strip() for value in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if value.strip()]
    return configured or ["http://localhost:3000", "http://localhost:5173"]


app = FastAPI(
    title="Trading Desk Frontend API",
    version="2.0.0",
    docs_url="/docs" if os.environ.get("API_DOCS_ENABLED", "").lower() == "true" else None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "If-Match", "X-Request-ID"],
)


@app.exception_handler(HTTPException)
async def contract_http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "meta" in exc.detail and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return response


def _http_error(request: Request, status: int, code: str, message: str, *, retryable: bool = False):
    raise HTTPException(
        status_code=status,
        detail=error_payload(code, message, retryable=retryable, request_id=request.state.request_id),
    )


def current_identity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> VerifiedIdentity:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        _http_error(request, 401, "unauthorized", "Sign in to access this workspace.")
    try:
        return verify_access_token(token.strip())
    except TokenVerificationError as exc:
        _http_error(request, 401, "unauthorized", str(exc))


Identity = Annotated[VerifiedIdentity, Depends(current_identity)]


def _run(request: Request, operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except ValueError as exc:
        _http_error(request, 422, "invalid_ticker", str(exc))
    except api_repository.NotFoundError as exc:
        _http_error(request, 404, "not_found", str(exc))
    except api_repository.ConflictError as exc:
        _http_error(request, 409, "workspace_conflict", str(exc), retryable=True)
    except HTTPException:
        raise
    except Exception:
        _http_error(request, 503, "service_unavailable", "Trading Desk data is temporarily unavailable.", retryable=True)


@app.get("/v1/health")
def health():
    return api_repository.health()


@app.get("/v1/regime")
def regime(request: Request):
    return _run(request, api_repository.regime)


@app.get("/v1/decisions/{ticker}")
def decision(ticker: str, request: Request):
    return _run(request, api_repository.decision, ticker)


@app.post("/v1/decisions/{ticker}/requests", status_code=202)
def request_decision(ticker: str, request: Request, identity: Identity, response: Response):
    result = _run(request, api_repository.request_decision, ticker, identity.user_id)
    if result.get("status") == "ready":
        response.status_code = 200
    return result


@app.get("/v1/analysis-requests/{job_id}")
def analysis_request(job_id: str, request: Request, identity: Identity):
    return _run(request, api_repository.analysis_request, job_id, identity.user_id)


@app.get("/v1/workspace")
def workspace(request: Request, identity: Identity):
    return _run(request, api_repository.workspace, identity.user_id)


@app.patch("/v1/workspace")
def patch_workspace(payload: dict[str, Any], request: Request, identity: Identity):
    return _run(request, api_repository.patch_workspace, identity.user_id, payload)


@app.get("/v1/watchlist")
def watchlist(request: Request, identity: Identity):
    return _run(request, api_repository.watchlist, identity.user_id)


@app.put("/v1/watchlist/{ticker}")
def add_watchlist_ticker(ticker: str, request: Request, identity: Identity):
    return _run(request, api_repository.set_watchlist_ticker, identity.user_id, ticker, present=True)


@app.delete("/v1/watchlist/{ticker}", status_code=204)
def remove_watchlist_ticker(ticker: str, request: Request, identity: Identity):
    _run(request, api_repository.set_watchlist_ticker, identity.user_id, ticker, present=False)
    return Response(status_code=204)


@app.get("/v1/attention")
def attention(request: Request, identity: Identity):
    return _run(request, api_repository.attention, identity.user_id)


@app.get("/v1/portfolio")
def portfolio(request: Request, identity: Identity):
    return _run(request, api_repository.portfolio, identity.user_id)


@app.get("/v1/calibration")
def calibration(request: Request, identity: Identity):
    return _run(request, api_repository.calibration)
