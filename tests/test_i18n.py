"""Language defaults, catalog integrity, persistence, and async isolation."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tdl_bot.bot.application import BotApplication
from tdl_bot.bot.commands import ADMIN_HELP, COMMANDS, HELP, USER_HELP
from tdl_bot.bot.progress import ProgressMessages
from tdl_bot.core.errors import UserError
from tdl_bot.core.i18n import (
    CATALOGS,
    current_language,
    saved_label,
    tr,
    use_language,
)
from tdl_bot.integrations.tdl.gate import UpdateGate
from tdl_bot.runtime.engine import EngineServer, code_version, exchange
from tdl_bot.storage.models import Target
from tdl_bot.storage.store import Store
from tdl_bot.transfers.accounts import AccessPolicy, AccountPool, Executor
from tdl_bot.transfers.jobs import Job
from tdl_bot.transfers.service import TransferService


def test_default_and_unknown_languages_are_english():
    assert current_language() == "en"
    assert tr("Cancel") == "Cancel"
    with use_language("unknown"):
        assert tr("Cancel") == "Cancel"
    with use_language("zh"):
        assert tr("Cancel") == "取消"
        assert (
            tr("Future untranslated message") == "Future untranslated message"
        )
    assert current_language() == "en"


def test_user_language_persists_without_changing_other_accounts(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    assert store.language(1) == store.language(2) == "en"
    store.set_language(1, "zh")
    with pytest.raises(ValueError, match="Unsupported"):
        store.set_language(2, "unknown")
    store.close()
    store = Store(path, migrate=False)
    assert store.language(1) == "zh"
    assert store.language(2) == "en"
    store.close()


async def test_concurrent_languages_and_parameter_values_are_isolated():
    async def render(language):
        with use_language(language):
            await asyncio.sleep(0)
            return tr("Your user ID: {p0}", p0="{unchanged}")

    assert await asyncio.gather(render("zh"), render("en")) == [
        "你的用户 ID：{unchanged}",
        "Your user ID: {unchanged}",
    ]
    assert current_language() == "en"
    assert set(CATALOGS) >= {"en", "zh"}


@pytest.fixture
def bilingual(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.set_target(1, Target("me"))
    policy = AccessPolicy(frozenset({1, 2}), frozenset({1}), store=store)
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=90)),
        edit_message_text=AsyncMock(),
    )

    async def download(urls, directory):
        (directory / "10_photo.jpg").write_bytes(b"photo")

    tdl = SimpleNamespace(
        gate=UpdateGate(), download=AsyncMock(side_effect=download)
    )
    media = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                messages=[SimpleNamespace(id=10, document=None)],
                urls=["https://t.me/example/10"],
            )
        ),
        upload=AsyncMock(return_value=[100]),
    )
    service = TransferService(
        AccountPool(Executor("shared", tdl, media)),
        store,
        tmp_path / "jobs",
        AsyncMock(),
        policy=policy,
        workers=2,
    )
    service.jobs_dir.mkdir()
    app = BotApplication(bot, SimpleNamespace(), store, tdl, service)
    yield app, store, service
    store.close()


def event(user_id):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message_id=70,
        answer=AsyncMock(),
    )


@pytest.mark.parametrize(
    "invalid", ["denied", "group", "owner", "code", "missing"]
)
async def test_language_buttons_validate_access_and_private_chat(
    bilingual, invalid
):
    app, store, _ = bilingual
    if invalid == "denied":
        store.ban_member(1, 2, 86400)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=2),
        data="language:unknown" if invalid == "code" else "language:zh",
        message=None
        if invalid == "missing"
        else SimpleNamespace(
            chat=SimpleNamespace(
                type="group" if invalid == "group" else "private",
                id=1 if invalid == "owner" else 2,
            ),
            message_id=80,
        ),
        answer=AsyncMock(),
    )
    await app.select_language(query)
    assert query.answer.call_args.kwargs["show_alert"]
    assert not store.has_language(2)
    app.bot.edit_message_text.assert_not_awaited()


async def test_background_jobs_and_cancellation_use_submitters_language(
    bilingual,
):
    _, store, service = bilingual
    store.set_language(2, "zh")
    service.start()
    try:
        one = await service.submit(1, "https://t.me/example/10")
        # Bot handlers supply this context to the initial queue reply.
        with use_language("zh"):
            two = await service.submit(2, "https://t.me/example/20")
        await asyncio.wait_for(
            asyncio.gather(one.done.wait(), two.done.wait()), 5
        )
        messages = {
            user: [
                call.args[1]
                for call in service.notify.call_args_list
                if call.args[0].user_id == user
            ]
            for user in (1, 2)
        }
        assert messages[1][0] == "1/5 Download task queued."
        assert messages[2][0] == "1/5 下载任务已加入队列。"
        assert any("5/5 Complete" in text for text in messages[1])
        assert any("5/5 完成" in text for text in messages[2])
        await service._cancelled(Job(2, "https://t.me/example/30", Target("2")))
        assert "已取消" in service.notify.call_args.args[1]
    finally:
        await service.close()


async def test_progress_rechecks_language_after_preference_change(bilingual):
    _, store, service = bilingual
    job = Job(2, "https://t.me/example/10", Target("2"))
    text = tr("3/5 Downloading {p0} media files.", p0=2)
    store.set_language(2, "zh")
    await service._notify(job, text)
    assert service.notify.call_args.args[1] == "3/5 正在下载 2 个媒体。"
    with use_language("zh"):
        assert saved_label("Queued") == "排队中"
    assert saved_label("排队中") == "Queued"


async def test_buttons_and_error_replies_use_saved_language(bilingual):
    app, store, service = bilingual
    store.set_language(2, "zh")
    service.notify = ProgressMessages(app.bot)
    for user_id, cancel, expired, failure in (
        (1, "Cancel", "Task ended or button expired.", "Command failed."),
        (2, "取消", "任务已结束或按钮已失效。", "命令失败"),
    ):
        job = Job(user_id, "https://t.me/example/10", Target(str(user_id)))
        await service._notify(job, tr("1/5 Download task queued."))
        markup = app.bot.send_message.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == cancel
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=user_id),
                message_id=90,
            ),
            data="cancel:missing",
            answer=AsyncMock(),
        )
        await app.tasks.cancel(query)
        assert query.answer.call_args.args[0] == expired
        user = event(user_id)
        await app.error(
            SimpleNamespace(
                update=SimpleNamespace(message=user),
                exception=RuntimeError("private diagnostic"),
            )
        )
        assert failure in user.answer.call_args.args[0]
        assert "private diagnostic" not in user.answer.call_args.args[0]


def test_catalog_changes_are_detected_for_manual_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tdl_bot.runtime.engine.__file__",
        str(tmp_path / "runtime" / "engine.py"),
    )
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "engine.py").write_text("# engine")
    directory = tmp_path / "locales"
    directory.mkdir()
    catalog = directory / "zh.json"
    catalog.write_text('{"name": "Chinese"}')
    before = code_version()
    catalog.write_text('{"name": "Simplified Chinese"}')
    assert code_version() != before
    before = code_version()
    nested = tmp_path / "transfers"
    nested.mkdir()
    source = nested / "service.py"
    source.write_text("# updated transfer logic")
    assert code_version() != before


@pytest.mark.parametrize("unexpected", [False, True])
async def test_rpc_errors_preserve_request_language(
    tmp_path, monkeypatch, unexpected
):
    engine = EngineServer(None, None)

    async def dispatch(method, args):
        await asyncio.sleep(0)
        if unexpected:
            raise RuntimeError("not for users")
        raise UserError(tr("You cannot manage this task."))

    monkeypatch.setattr(engine, "dispatch", dispatch)
    path = tmp_path / "rpc.sock"
    server = await asyncio.start_unix_server(engine.client, path)

    async def call(language):
        with use_language(language), pytest.raises(UserError) as error:
            await exchange(path, {"method": "test", "args": {}})
        return str(error.value)

    async with server:
        english, chinese = await asyncio.gather(call("en"), call("zh"))
    if unexpected:
        assert (
            english == "Task engine operation failed. Check the service logs."
        )
        assert chinese == "任务引擎操作失败，请检查服务日志。"
    else:
        assert english == "You cannot manage this task."
        assert chinese == "无权管理此任务。"


def test_all_user_templates_have_chinese_translations():
    messages = CATALOGS["zh"]["messages"]
    sources = {HELP, ADMIN_HELP, USER_HELP, *(row[2] for row in COMMANDS)}
    for path in Path("src/tdl_bot").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                sources.add(node.args[0].value)
    assert sources <= messages.keys()
