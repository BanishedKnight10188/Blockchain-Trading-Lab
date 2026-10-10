"""Independent background archive keeps bulk history off the trading wallet writer."""

from contextlib import closing

from agent_platform.domain.background import BackgroundRequest, BackgroundResult
from agent_platform.domain.model_diagnostics import ModelDiagnostic

from .events import SqliteStore


class SqliteBackgroundStore(SqliteStore):
    async def initialize(self):
        def create():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db, db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS background_records (request_id TEXT PRIMARY KEY, "
                    "session_id TEXT NOT NULL, style_revision INTEGER NOT NULL, "
                    "symbol TEXT NOT NULL, "
                    "request TEXT NOT NULL, result TEXT, failure TEXT, diagnostic TEXT)"
                )
                if "diagnostic" not in {
                    row[1] for row in db.execute("PRAGMA table_info(background_records)")
                }:
                    db.execute("ALTER TABLE background_records ADD COLUMN diagnostic TEXT")

        await self._io(create)

    async def prepare(self, request):
        request = BackgroundRequest.model_validate_json(request.model_dump_json())

        def write():
            with closing(self._connect()) as db, db:
                old = db.execute(
                    "SELECT request FROM background_records WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
                if old is not None:
                    if old["request"] != request.model_dump_json():
                        raise ValueError("background identity conflicts")
                    return
                db.execute(
                    "INSERT INTO background_records"
                    "(request_id,session_id,style_revision,symbol,request) VALUES(?,?,?,?,?)",
                    (
                        request.request_id,
                        request.session_id,
                        request.style_revision,
                        request.symbol,
                        request.model_dump_json(),
                    ),
                )

        await self._io(write)

    async def finish(self, request, result=None, failure=None, diagnostic=None):
        if diagnostic is not None:
            diagnostic = ModelDiagnostic.model_validate_json(diagnostic.model_dump_json())
        if result is not None:
            result = BackgroundResult.model_validate_json(result.model_dump_json())
            if result.request != request:
                raise ValueError("background answer identity conflicts")

        def write():
            with closing(self._connect()) as db, db:
                old = db.execute(
                    "SELECT request,result,failure,diagnostic FROM background_records "
                    "WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
                body = result.model_dump_json() if result else None
                details = diagnostic.model_dump_json() if diagnostic else None
                if old is None or old["request"] != request.model_dump_json():
                    raise ValueError("background request missing")
                if old["result"] is not None or old["failure"] is not None:
                    if (old["result"], old["failure"], old["diagnostic"]) != (
                        body,
                        failure,
                        details,
                    ):
                        raise ValueError("background completion conflicts")
                    return
                db.execute(
                    "UPDATE background_records SET result=?,failure=?,diagnostic=? "
                    "WHERE request_id=?",
                    (body, failure, details, request.request_id),
                )

        await self._io(write)

    async def latest(self, session_id, style_revision, symbol):
        def read():
            with closing(self._connect()) as db:
                rows = db.execute(
                    "SELECT result FROM background_records WHERE session_id=? AND style_revision=? "
                    "AND symbol=? AND result IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                    (session_id, style_revision, symbol),
                ).fetchone()
                return BackgroundResult.model_validate_json(rows["result"]) if rows else None

        return await self._io(read)
