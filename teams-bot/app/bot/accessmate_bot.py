"""AccessMate Bot Framework handler.

Bridges the SDK turn to the existing `process_activity(activity: dict)` logic: it publishes the
active TurnContext (so `teams_service` reply/card helpers send through the SDK) and passes the
activity as the same wire-format dict the raw-webhook path already consumes. This keeps the large
existing handler untouched while adopting the SDK for inbound auth + outbound I/O.
"""
import logging

from botbuilder.core import ActivityHandler, TurnContext

from app.bot.turn_state import current_turn

logger = logging.getLogger("teams_bot.accessmate_bot")


class AccessMateBot(ActivityHandler):
    async def on_turn(self, turn_context: TurnContext):
        token = current_turn.set(turn_context)
        try:
            from app.services.bot_handler import process_activity

            # Activity.serialize() yields the same camelCase wire JSON the raw path expects
            # (serviceUrl, replyToId, conversation.id, from, value, text, id, …).
            activity_dict = turn_context.activity.serialize()
            await process_activity(activity_dict)
        finally:
            current_turn.reset(token)
