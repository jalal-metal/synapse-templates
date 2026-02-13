import logging
from typing import Iterable, Any, Optional, Dict
from synapse.module_api import ModuleApi
from synapse.push import mailer
from synapse.types import StateMap
from synapse.push.push_types import RoomVars
from synapse.storage.databases.main.event_push_actions import EmailPushAction
from synapse.events import EventBase
from synapse.api.constants import EventTypes, Membership
from synapse.push.presentable_names import calculate_room_name

# Import helper function that is not exported but available in the module
from synapse.push.mailer import string_ordinal_total

logger = logging.getLogger(__name__)

class EmailAliasModule:
    def __init__(self, config: dict, api: ModuleApi):
        self.api = api
        logger.info("EmailAliasModule: Initializing and patching synapse.push.mailer.Mailer")
        self._patch_mailer()

    def _patch_mailer(self):
        # 1. Add the _get_room_alias helper method to the Mailer class
        async def _get_room_alias(self, room_state_ids: StateMap[str]) -> Optional[str]:
            """
            Retrieve the canonical alias for this room
            """
            # EventTypes.CanonicalAlias = "m.room.canonical_alias"
            event_id = room_state_ids.get(("m.room.canonical_alias", ""))
            if event_id:
                try:
                    ev = await self.store.get_event(event_id)
                    alias = ev.content.get("alias")
                    if isinstance(alias, str):
                        return alias
                except Exception as e:
                    logger.warning(f"Error fetching canonical alias: {e}")
            return None

        # 2. Redefine _get_room_vars to include canonical_alias
        async def _get_room_vars(
            self,
            room_id: str,
            user_id: str,
            notifs: Iterable[EmailPushAction],
            notif_events: dict[str, EventBase],
            room_state_ids: StateMap[str],
        ) -> RoomVars:
            """
            Generate the variables for notifications on a per-room basis.
            Patched to include canonical_alias.
            """

            # Check if one of the notifs is an invite event for the user.
            is_invite = False
            for n in notifs:
                ev = notif_events[n.event_id]
                if ev.type == EventTypes.Member and ev.state_key == user_id:
                    if ev.content.get("membership") == Membership.INVITE:
                        is_invite = True
                        break

            room_name = await calculate_room_name(self.store, room_state_ids, user_id)

            # Get canonical alias using our new helper
            canonical_alias = await self._get_room_alias(room_state_ids)

            room_vars: RoomVars = {
                "title": room_name,
                "hash": string_ordinal_total(room_id),  # See sender avatar hash
                "notifs": [],
                "invite": is_invite,
                "link": self._make_room_link(room_id),
                "avatar_url": await self._get_room_avatar(room_state_ids),
                "canonical_alias": canonical_alias, # INJECTED FIELD
            }

            if not is_invite:
                for n in notifs:
                    notifvars = await self._get_notif_vars(
                        n, user_id, notif_events[n.event_id], room_state_ids
                    )

                    # merge overlapping notifs together.
                    # relies on the notifs being in chronological order.
                    merge = False
                    if room_vars["notifs"] and "messages" in room_vars["notifs"][-1]:
                        prev_messages = room_vars["notifs"][-1]["messages"]
                        for message in notifvars["messages"]:
                            pm = list(
                                filter(lambda pm: pm["id"] == message["id"], prev_messages)
                            )
                            if pm:
                                if not message["is_historical"]:
                                    pm[0]["is_historical"] = False
                                merge = True
                            elif merge:
                                # we're merging, so append any remaining messages
                                # in this notif to the previous one
                                prev_messages.append(message)

                    if not merge:
                        room_vars["notifs"].append(notifvars)

            return room_vars

        # Apply the patches
        mailer.Mailer._get_room_alias = _get_room_alias
        mailer.Mailer._get_room_vars = _get_room_vars
        logger.info("EmailAliasModule: Patched Mailer._get_room_vars and added Mailer._get_room_alias")
