"""Bot Framework adapter (classic BotFrameworkAdapter — FastAPI-compatible, no aiohttp needed).

Built lazily so importing this module never fails when the SDK path is disabled or the SDK is
absent. `get_adapter()` / `get_bot()` return singletons used by the /api/messages endpoint.
"""
import logging

from app.config import settings

logger = logging.getLogger("teams_bot.bf_adapter")

_adapter = None
_bot = None


def get_adapter():
    global _adapter
    if _adapter is None:
        from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings

        _adapter = BotFrameworkAdapter(
            BotFrameworkAdapterSettings(
                app_id=settings.teams_app_id or "",
                app_password=settings.teams_app_password or "",
            )
        )

        async def on_error(turn_context, error):  # noqa: ANN001
            logger.error("Bot Framework turn error: %s", error, exc_info=True)
            try:
                await turn_context.send_activity("Sorry — something went wrong. Please try again.")
            except Exception:
                pass

        _adapter.on_turn_error = on_error
        logger.info("BotFrameworkAdapter initialised (app_id=%s)", settings.teams_app_id)
    return _adapter


def get_bot():
    global _bot
    if _bot is None:
        from app.bot.accessmate_bot import AccessMateBot

        _bot = AccessMateBot()
    return _bot
