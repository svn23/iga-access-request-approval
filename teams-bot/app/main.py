# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
load_dotenv()

import logging
import asyncio
import uuid
import time
# pyrefly: ignore [missing-import]
from fastapi import FastAPI, Request
# pyrefly: ignore [missing-import]
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api.router import api_router
from app.logging_config import setup_logging

# Setup structured logging
setup_logging()
logger = logging.getLogger("teams_bot")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.db_service import init_db
    from app.services.mcp_service import fetch_mcp_tools
    try:
        init_db()
        logger.info("Database tables verified and initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database tables: {e}", exc_info=True)
    try:
        await fetch_mcp_tools()
    except Exception as e:
        logger.error(f"Failed to fetch MCP tools: {e}")
    from app.services.catalog_service import catalog_sync_loop
    catalog_task = asyncio.create_task(catalog_sync_loop(), name="midpoint-catalog-sync")
    try:
        yield
    finally:
        catalog_task.cancel()
        try:
            await catalog_task
        except asyncio.CancelledError:
            pass

app = FastAPI(
    title="AccessMate Teams Bot API",
    description="Secure access-request and approval workflow backend for Microsoft Teams",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    # Teams posts server-to-server; browser CORS is not needed for the webhook.
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization"],
)

# Request/Response logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start_time = time.time()

    # Log incoming request
    logger.info(
        f"Incoming {request.method} {request.url.path}",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else "unknown",
            "headers": dict(request.headers),
        }
    )

    try:
        response = await call_next(request)
        duration = time.time() - start_time

        # Log response
        logger.info(
            f"Response {request.method} {request.url.path} -> {response.status_code}",
            extra={
                "request_id": request_id,
                "status_code": response.status_code,
                "duration": f"{duration:.3f}s",
            }
        )

        return response
    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            f"Error handling {request.method} {request.url.path}: {str(e)}",
            extra={
                "request_id": request_id,
                "duration": f"{duration:.3f}s",
            },
            exc_info=True
        )
        raise

# Register master router
app.include_router(api_router)

if __name__ == "__main__":
    # pyrefly: ignore [missing-import]
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
