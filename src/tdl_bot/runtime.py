"""Private local RPC separates Bot polling from durable transfer lifetimes."""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from tdl_bot.accounts import AccessPolicy
from tdl_bot.errors import UserError
from tdl_bot.i18n import current_language, tr, use_language
from tdl_bot.links import JoinLink
from tdl_bot.service import Job
from tdl_bot.store import Store, Target

_LOGGER = logging.getLogger(__name__)
_LIMIT = 1024 * 1024


def code_version() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted([*root.glob("*.py"), *root.glob("locales/*.json")]):
        digest.update(path.relative_to(root).as_posix().encode())
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"removed")
    return digest.hexdigest()


def spawn_process(command: str, **kwargs):
    return subprocess.Popen(
        [sys.executable, "-c", "from tdl_bot.cli import main; main()", command],
        **kwargs,
    )


async def exchange(path: Path, request):
    reader, writer = await asyncio.open_unix_connection(str(path), limit=_LIMIT)
    try:
        request = {**request, "language": current_language()}
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        if "error" in response:
            raise UserError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


async def ensure_engine(settings):
    path = settings.data_dir / "runtime.sock"
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = None
    for _ in range(300):
        try:
            await exchange(path, {"method": "ping", "args": {}})
            return path
        except (OSError, ValueError, KeyError):
            pass
        # Wait for an old generation to release its session locks before
        # launching the replacement. Never unlink its live socket here.
        busy = False
        with (settings.data_dir / ".instance.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                busy = True
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
        if process is not None and process.poll() is not None and not busy:
            raise UserError(
                tr("Task engine failed to start. Check data/engine.log.")
            )
        if process is None and not busy:
            with (settings.data_dir / "engine.log").open(
                "ab", buffering=0
            ) as output:
                os.chmod(output.name, 0o600)
                process = spawn_process(
                    "engine",
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
        await asyncio.sleep(0.2)
    raise UserError(
        tr(
            "Task engine startup timed out. Check data/engine.log and "
            "existing processes."
        )
    )


class EngineServer:
    def __init__(self, service, settings):
        self.service = service
        self.settings = settings
        self.version = code_version()
        self.reload_requested = False
        self.calls = 0

    async def dispatch(self, method, args):
        service = self.service
        if method == "shutdown":
            service._closing = True
            await service.close()
            self.reload_requested = True
            return None
        if method == "ping":
            if args.get("version") and args["version"] != self.version:
                self.reload_requested = True
            return {"version": self.version, "pid": os.getpid()}
        if method in {"submit", "retry"}:
            return (await getattr(service, method)(**args)).payload()
        if method == "discard_pending":
            return service.discard_pending(**args)
        if method in {"cancel", "cancel_submitted", "reconcile_permissions"}:
            return await getattr(service, method)(**args)
        executor = service.accounts.get(args.pop("executor_key", "shared"))
        if method == "update":
            return await executor.tdl.update(**args)
        async with executor.tdl.gate.job():
            if method == "validate_target":
                await executor.media.validate_target(Target(**args["target"]))
                return None
            if method == "join":
                await executor.media.join(JoinLink(**args["link"]))
                return None
            if method == "resolve_user":
                user = await executor.media.resolve_user(args["value"])
                return {
                    name: getattr(user, name)
                    for name in (
                        "id",
                        "username",
                        "first_name",
                        "last_name",
                    )
                }
        raise UserError(tr("Unsupported local operation."))

    async def client(self, reader, writer):
        self.calls += 1
        language = "en"
        try:
            try:
                request = json.loads(await reader.readline())
                language = request.get("language", "en")
                with use_language(language):
                    result = await self.dispatch(
                        request["method"], request["args"]
                    )
                response = {"result": result}
            except UserError as exc:
                response = {"error": str(exc)}
            except Exception as exc:
                _LOGGER.warning(
                    "Local operation failed: %s", type(exc).__name__
                )
                with use_language(language):
                    response = {
                        "error": tr(
                            "Task engine operation failed. "
                            "Check the service logs."
                        )
                    }
            try:
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass  # The frontend can restart while this operation finishes.
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
        finally:
            self.calls -= 1

    async def serve(self):
        path = self.settings.data_dir / "runtime.sock"
        # The engine instance lock is acquired before removing a stale socket.
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self.client, path, limit=_LIMIT
        )
        path.chmod(0o600)
        try:
            async with server:
                while True:
                    await asyncio.sleep(0.5)
                    await self.service.reconcile_permissions()
                    if (
                        self.reload_requested
                        and not self.service._jobs
                        and not self.calls
                    ):
                        return
        finally:
            path.unlink(missing_ok=True)


class RemoteGate:
    @asynccontextmanager
    async def job(self):
        # The actual gate is held inside each engine operation.
        yield


class RemoteTdl:
    def __init__(self, runtime):
        self.runtime = runtime
        self.gate = RemoteGate()

    async def update(self, version="latest"):
        return await self.runtime.call("update", version=version)


class RemoteMedia:
    def __init__(self, runtime, key):
        self.runtime = runtime
        self.key = key

    async def resolve_user(self, value):
        result = await self.runtime.call(
            "resolve_user", value=value, executor_key=self.key
        )
        return SimpleNamespace(**result)

    async def validate_target(self, target):
        await self.runtime.call(
            "validate_target",
            target={
                "chat": target.chat,
                "topic_id": target.topic_id,
            },
            executor_key=self.key,
        )

    async def join(self, link):
        await self.runtime.call(
            "join",
            link={
                "username": link.username,
                "invite_hash": link.invite_hash,
            },
            executor_key=self.key,
        )


class RemoteAccounts:
    def __init__(self, runtime, settings):
        self.runtime = runtime
        self.personal = {item.operator_id for item in settings.account_bindings}
        self.shared = self.get("shared")

    def get(self, key):
        owner = int(key.split(":")[1]) if key.startswith("operator:") else None
        return SimpleNamespace(
            key=key,
            owner_id=owner,
            media=RemoteMedia(self.runtime, key),
            tdl=self.runtime.tdl,
        )

    def select(self, operator):
        return self.get(
            f"operator:{operator}" if operator in self.personal else "shared"
        )


class RemoteService:
    def __init__(self, settings, store: Store):
        self.settings = settings
        self.store = store
        self.policy = AccessPolicy(
            settings.allowed_users, settings.admin_users, store=store
        )
        self.tdl = RemoteTdl(self)
        self.accounts = RemoteAccounts(self, settings)

    async def call(self, method, **args):
        path = await ensure_engine(self.settings)
        try:
            return await exchange(path, {"method": method, "args": args})
        except (OSError, ValueError, KeyError):
            # Never blindly retry a write whose acknowledgement was lost.
            raise UserError(
                tr("The connection restarted. Check /jobs before retrying.")
            ) from None

    async def submit(self, user_id, url, *, source_message_id=None):
        return Job.restore(
            await self.call(
                "submit",
                user_id=user_id,
                url=url,
                source_message_id=source_message_id,
            )
        )

    async def retry(self, user_id, pending_id, *, source_message_id=None):
        return Job.restore(
            await self.call(
                "retry",
                user_id=user_id,
                pending_id=pending_id,
                source_message_id=source_message_id,
            )
        )

    def jobs(self, user_id):
        return [
            Job.restore(payload)
            for payload in self.store.active_job_payloads()
            if not payload["finished"]
            and self.policy.can_manage(user_id, payload["owner_id"])
        ]

    def current_job(self, user_id, job_id):
        matches = [
            job
            for job in self.jobs(user_id)
            if len(job_id) >= 8 and job.id.startswith(job_id)
        ]
        return matches[0] if len(matches) == 1 else None

    def pending_items(self, user_id):
        return [
            item
            for item in self.store.pending_items(
                include_archived=self.policy.is_admin(user_id)
            )
            if self.policy.can_manage(user_id, item.destination.owner_id)
        ]

    def pending_record(self, user_id, pending_id=None):
        items = self.pending_items(user_id)
        if pending_id is None and len(items) == 1:
            return items[0]
        for item in items:
            if item.id == pending_id:
                return item
        raise UserError(tr("Use /pending to find a retry ID you can manage."))

    async def discard_pending(self, user_id, pending_id):
        await self.call(
            "discard_pending", user_id=user_id, pending_id=pending_id
        )

    async def cancel(self, user_id, job_id):
        return await self.call("cancel", user_id=user_id, job_id=job_id)

    async def cancel_submitted(self, admin_id, operator_id):
        await self.call(
            "cancel_submitted", admin_id=admin_id, operator_id=operator_id
        )

    async def reconcile_permissions(self):
        await self.call("reconcile_permissions")
