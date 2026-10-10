"""Intent and protection are committed before an order can be dispatched."""

from contextlib import closing

from agent_platform.domain.position_protection import PositionProtection
from agent_platform.domain.trade_intents import TradeIntent
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .events import SqliteStore


def load_protection(db, key):
    row = db.execute(
        "SELECT body FROM position_protections WHERE protection_id=?", (key,)
    ).fetchone()
    if row is None:
        raise ValueError("protection_not_found")
    return PositionProtection.model_validate_json(row[0])


class SqliteProtectionStore(SqliteStore):
    async def prepare(self, intent):
        intent = TradeIntent.model_validate_json(intent.model_dump_json())

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT body FROM agent_trade_intents WHERE intent_id=?", (intent.intent_id,)
                ).fetchone()
                if old:
                    if TradeIntent.model_validate_json(old[0]) != intent:
                        raise EventIdentityConflict("intent_identity_conflict")
                    return (
                        load_protection(db, intent.intent_id) if intent.action != "reduce" else None
                    )
                if (
                    intent.action != "reduce"
                    and db.execute(
                        (
                            "SELECT 1 FROM position_protections WHERE account_ref=? AND "
                            "status!='closed'"
                        ),
                        (intent.scope.account_ref,),
                    ).fetchone()
                ):
                    raise ValueError("existing_position_protection")
                db.execute(
                    "INSERT INTO agent_trade_intents VALUES(?,?,?,?)",
                    (
                        intent.intent_id,
                        intent.evidence.lane_id,
                        intent.scope.account_ref,
                        intent.model_dump_json(),
                    ),
                )
                if intent.action == "reduce":
                    return None
                value = PositionProtection(protection_id=intent.intent_id, intent=intent)
                db.execute(
                    "INSERT INTO position_protections VALUES(?,?,?,?,?)",
                    (
                        value.protection_id,
                        intent.scope.account_ref,
                        1,
                        value.status,
                        value.model_dump_json(),
                    ),
                )
                return value

        return await self._io(write)

    async def get(self, protection_id):
        def read():
            with closing(self._connect()) as db:
                return load_protection(db, protection_id)

        return await self._io(read)

    async def get_intent(self, intent_id):
        def read():
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT body FROM agent_trade_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("intent_not_found")
                return TradeIntent.model_validate_json(row[0])

        return await self._io(read)

    async def save(self, protection, expected_revision):
        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                old = load_protection(db, protection.protection_id)
                if old.revision != expected_revision:
                    raise RevisionConflict("protection_changed")
                if old.intent != protection.intent:
                    raise ValueError("protection_intent_immutable")
                value = PositionProtection.model_validate(
                    protection.model_dump() | {"revision": old.revision + 1}
                )
                db.execute(
                    (
                        "UPDATE position_protections SET revision=?,status=?,body=? WHERE "
                        "protection_id=?"
                    ),
                    (value.revision, value.status, value.model_dump_json(), value.protection_id),
                )
                return value

        return await self._io(write)

    async def recover(self, scope):
        def read():
            with closing(self._connect()) as db:
                return tuple(
                    PositionProtection.model_validate_json(r[0])
                    for r in db.execute(
                        (
                            "SELECT body FROM position_protections WHERE account_ref=? AND "
                            "status!='closed'"
                        ),
                        (scope.account_ref,),
                    )
                )

        return await self._io(read)
