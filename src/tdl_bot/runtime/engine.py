"""Private local RPC separates Bot polling from durable transfer lifetimes."""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from tdl_bot.core.config import runtime_directory
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import (
    current_language,
    error_message,
    pack_message,
    render,
    tr,
    use_language,
)
from tdl_bot.runtime import locks
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy
from tdl_bot.transfers.jobs import Job

_LOGGER = logging.getLogger(__name__)
_LIMIT = 1024 * 1024


def code_version() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parents[1]
    for path in sorted([*root.rglob("*.py"), *root.glob("locales/*.json")]):
        digest.update(path.relative_to(root).as_posix().encode())
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"removed")
    return digest.hexdigest()


def spawn_process(command: str, **kwargs):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tdl_bot import main; main()",
            command,
        ],
        **kwargs,
    )


async def exchange(path: Path, request):
    assert sys.platform != "win32", "Engine RPC requires Unix sockets"
    reader, writer = await asyncio.open_unix_connection(str(path), limit=_LIMIT)
    try:
        request = {**request, "language": current_language()}
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        if "error" in response:
            raise UserError(
                render(response.get("error_template", response["error"]))
            )
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


async def ensure_engine(settings):
    path = runtime_directory(settings) / "runtime.sock"
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = None
    for _ in range(300):
        try:
            await exchange(path, {"method": "ping", "args": {}})
            return path
        except (OSError, ValueError, KeyError):
            pass
        # Wait for an old generation to release its session locks before
        # launching the replacement. Never unlink its live socket here.
        busy = locks.busy(settings.data_dir / ".instance.lock")
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


async def maintain_engine(settings, *, interval=5):
    """Restart an idle engine after a manual handoff without watching code."""
    while True:
        try:
            await ensure_engine(settings)
        except (UserError, OSError):
            _LOGGER.warning("Task engine unavailable; waiting to reconnect")
        await asyncio.sleep(interval)


class EngineServer:
    def __init__(self, service, settings):
        self.service = service
        self.settings = settings
        self.version = code_version()
        self.reload_requested = False
        self.calls = 0
        self.shutdown_task = None

    async def dispatch(self, method, args):
        service = self.service
        if method == "backup":
            return await service.backups.execute(**args)
        if method == "shutdown":
            if self.shutdown_task is None:
                service._closing = True
                if getattr(service, "backups", None) is not None:
                    service.backups.quiesce()
                self.shutdown_task = asyncio.create_task(self._shutdown())
            await asyncio.shield(self.shutdown_task)
            return None
        if method == "ping":
            if args.get("reload") or (
                args.get("version") and args["version"] != self.version
            ):
                self.reload_requested = True
            return {"version": self.version, "pid": os.getpid()}
        if method in {"submit", "retry"}:
            return (await getattr(service, method)(**args)).payload()
        if method == "join_chat":
            return await service.join(**args)
        if method == "execution_accounts":
            return await service.execution_accounts(**args)
        if method == "discard_pending":
            return service.discard_pending(**args)
        if method in {"cancel", "cancel_submitted", "reconcile_permissions"}:
            return await getattr(service, method)(**args)
        executor = service.accounts.get(args.pop("executor_key", "shared"))
        if method == "tdl_version":
            async with executor.tdl.gate.job():
                return await executor.tdl.version()
        if method == "update":
            return await executor.tdl.update(**args)
        async with executor.tdl.gate.job():
            if method == "validate_target":
                await executor.media.validate_target(Target(**args["target"]))
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

    async def _shutdown(self):
        if getattr(self.service, "backups", None) is not None:
            await self.service.backups.close()
        await self.service.close()
        self.reload_requested = True

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
                response = {"error": error_message(exc)}
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
            if "error" in response:
                response["error_template"] = pack_message(response["error"])
                response["error"] = str(response["error"])
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
        assert sys.platform != "win32", "Engine RPC requires Unix sockets"
        path = runtime_directory(self.settings) / "runtime.sock"
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
                    backups = getattr(self.service, "backups", None)
                    if backups is not None:
                        await backups.reconcile_permissions()
                    if backups is not None and self.reload_requested:
                        backups.quiesce()
                    if (
                        self.reload_requested
                        and not self.service._jobs
                        and not self.calls
                        and (backups is None or not backups.busy)
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

    async def version(self):
        return await self.runtime.call("tdl_version")

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


class RemoteAccounts:
    def __init__(self, runtime):
        self.runtime = runtime
        self.shared = self.get("shared")

    def catalog(self):
        return self.runtime.store.execution_catalog()

    def get(self, key):
        owner = int(key.split(":")[1]) if key.startswith("operator:") else None
        return SimpleNamespace(
            key=key,
            owner_id=owner,
            media=RemoteMedia(self.runtime, key),
            tdl=self.runtime.tdl,
        )

    def can_use(self, operator, key, *, admin=False):
        return key == "shared" or key == f"operator:{operator}" or admin

    def select(self, operator):
        preferred = (
            self.runtime.store.executor_choice(operator)
            if self.runtime.policy.is_admin(operator)
            else None
        )
        keys = {item["key"] for item in self.catalog()}
        key = preferred or (
            f"operator:{operator}"
            if f"operator:{operator}" in keys
            else "shared"
        )
        if preferred and preferred not in keys:
            raise UserError(
                tr(
                    "The selected execution account is unavailable. "
                    "Use /account auto."
                )
            )
        return self.get(key)


class RemoteService:
    def __init__(self, settings, store: Store):
        self.settings = settings
        self.store = store
        self.policy = AccessPolicy(
            settings.allowed_users, settings.admin_users, store=store
        )
        self.tdl = RemoteTdl(self)
        self.accounts = RemoteAccounts(self)

    async def call(self, method, **args):
        path = await ensure_engine(self.settings)
        try:
            return await exchange(path, {"method": method, "args": args})
        except (OSError, ValueError, KeyError):
            # Never blindly retry a write whose acknowledgement was lost.
            raise UserError(
                tr("The connection restarted. Check /jobs before retrying.")
            ) from None

    async def join(self, user_id, url, executor_key):
        return await self.call(
            "join_chat", user_id=user_id, url=url, executor_key=executor_key
        )

    async def backup(self, user_id, kind, **options):
        return await self.call("backup", user_id=user_id, kind=kind, **options)

    def execution_account(self, user_id):
        return self.accounts.select(user_id)

    async def execution_accounts(self, user_id, choice=None):
        return await self.call(
            "execution_accounts", user_id=user_id, choice=choice
        )

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
            and self.accounts.can_use(
                user_id, item.executor_key, admin=self.policy.is_admin(user_id)
            )
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
