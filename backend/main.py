"""Stratovex — FastAPI backend entry point."""

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from jose import JWTError, jwt

from database import init_db
from config import SECRET_KEY, ALGORITHM
from ws_manager import manager
from scheduler import run_scheduler
from routes_auth import router as auth_router
from routes_market import router as market_router
from routes_trades import router as trades_router
from routes_trades import router_trades as trades_roll_router
from routes_analytics import router as analytics_router
from routes_alerts import router as alerts_router
from routes_suggestions import router as suggestions_router
from routes_strategies import router as strategies_router
from routes_news import router as news_router
from routes_futures import router as futures_router
from routes_stocks import router as stocks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    # Start background scheduler
    task = asyncio.create_task(run_scheduler(manager))
    yield
    # Shutdown
    task.cancel()


app = FastAPI(title="Stratovex", version="1.0.0", lifespan=lifespan)

# API routes
app.include_router(auth_router)
app.include_router(market_router)
app.include_router(trades_router)
app.include_router(trades_roll_router)
app.include_router(analytics_router)
app.include_router(alerts_router)
app.include_router(suggestions_router)
app.include_router(strategies_router)
app.include_router(news_router)
app.include_router(futures_router)
app.include_router(stocks_router)


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    # Verify token
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        await websocket.close(code=4001)
        return

    await manager.connect(websocket, user_id)
    try:
        while True:
            # Keep connection alive, handle client messages if needed
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)


# Serve frontend static files
app.mount("/css", StaticFiles(directory="../frontend/css"), name="css")
app.mount("/js", StaticFiles(directory="../frontend/js"), name="js")
app.mount("/pages", StaticFiles(directory="../frontend/pages"), name="pages")


@app.get("/favicon.png")
@app.get("/favicon.ico")
async def serve_favicon():
    return FileResponse("../frontend/favicon.png", media_type="image/png")


@app.get("/")
async def serve_index():
    return FileResponse("../frontend/index.html")


@app.get("/login")
async def serve_login():
    return FileResponse("../frontend/pages/login.html")


@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse("../frontend/pages/dashboard.html")


@app.get("/option-chain")
async def serve_option_chain():
    return FileResponse("../frontend/pages/option_chain.html")


@app.get("/positions")
async def serve_positions():
    return FileResponse("../frontend/pages/positions.html")


@app.get("/journal")
async def serve_journal():
    return FileResponse("../frontend/pages/journal.html")


@app.get("/analytics")
async def serve_analytics():
    return FileResponse("../frontend/pages/analytics.html")


@app.get("/alerts")
async def serve_alerts():
    return FileResponse("../frontend/pages/alerts.html")


@app.get("/strategy-builder")
async def serve_strategy_builder():
    return FileResponse("../frontend/pages/strategy_builder.html")


@app.get("/news-desk")
async def serve_news_desk():
    return FileResponse("../frontend/pages/news_desk.html")


@app.get("/futures")
async def serve_futures():
    return FileResponse("../frontend/pages/futures.html")


@app.get("/stock-options")
async def serve_stock_options():
    return FileResponse("../frontend/pages/stock_options.html")
