"""Media-group lookup, original captions and explicit join requests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors
from telethon.tl import functions, types

from tdl_bot.errors import AccessDenied, JoinPending, UserError
from tdl_bot.links import JoinLink, MessageLink
from tdl_bot.store import Target
from tdl_bot.telegram import TelegramMedia


def media_message(message_id, group_id=42, text="", entities=None):
    return SimpleNamespace(
        id=message_id,
        grouped_id=group_id,
        message=text,
        entities=entities or [],
        photo=object(),
        document=None,
        video=None,
    )


async def test_resolve_entire_album_from_uncaptioned_middle_member():
    bold = types.MessageEntityBold(0, 4)
    before = media_message(80, text="文字😀", entities=[bold])
    anchor = media_message(100)
    after = media_message(140)
    unrelated = media_message(141, group_id=99)

    class Client:
        get_input_entity = AsyncMock(return_value="peer")
        get_messages = AsyncMock(return_value=anchor)

        async def iter_messages(self, peer, *, reverse, **kwargs):
            for message in [after, unrelated] if reverse else [before]:
                yield message

    client = Client()
    media = TelegramMedia(client)
    group = await media.resolve(MessageLink("channel", 100))
    assert [message.id for message in group.messages] == [80, 100, 140]
    assert group.urls == [
        "https://t.me/channel/80",
        "https://t.me/channel/100",
        "https://t.me/channel/140",
    ]
    client.upload_file = AsyncMock(return_value=types.InputFile(1, 1, "x", ""))
    client.send_file = AsyncMock(return_value=[1, 2, 3])
    await media.upload(group, ["one", "two", "three"], Target("me", 77))
    kwargs = client.send_file.call_args.kwargs
    assert kwargs["caption"] == ["文字😀", "", ""]
    assert kwargs["formatting_entities"] == [[bold], [], []]
    assert kwargs["reply_to"] == 77
    assert kwargs["parse_mode"] is None


async def test_single_video_retains_mime_and_video_attributes():
    video = types.DocumentAttributeVideo(12, 640, 480, supports_streaming=True)
    message = media_message(5, group_id=None, text="video text")
    message.photo = None
    message.video = video
    message.document = SimpleNamespace(
        mime_type="video/mp4", attributes=[video], size=100
    )
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=message),
        upload_file=AsyncMock(return_value=types.InputFile(1, 1, "x", "")),
        send_file=AsyncMock(return_value=object()),
    )
    media = TelegramMedia(client)
    group = await media.resolve(MessageLink("channel", 5))
    await media.upload(group, ["video"], Target("me"))
    uploaded = client.send_file.call_args.args[1]
    assert isinstance(uploaded, types.InputMediaUploadedDocument)
    assert uploaded.mime_type == "video/mp4"
    assert uploaded.attributes == [video]
    assert client.send_file.call_args.kwargs["caption"] == "video text"


async def test_missing_message_is_not_misclassified_as_permission():
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=None),
    )
    with pytest.raises(UserError, match="不存在"):
        await TelegramMedia(client).resolve(MessageLink("channel", 5))


async def test_private_chat_without_dialog_requests_permission():
    class Client:
        get_input_entity = AsyncMock(side_effect=ValueError())

        async def iter_dialogs(self):
            if False:
                yield None

    with pytest.raises(AccessDenied):
        await TelegramMedia(Client()).resolve(MessageLink(-1000000123456, 5))


async def test_private_id_refreshes_dialog_cache():
    anchor = media_message(5, group_id=None)

    class Client:
        get_input_entity = AsyncMock(side_effect=ValueError())
        get_messages = AsyncMock(return_value=anchor)

        async def iter_dialogs(self):
            yield SimpleNamespace(id=-1000000123456, input_entity="cached")

    client = Client()
    group = await TelegramMedia(client).resolve(MessageLink(-1000000123456, 5))
    assert group.urls == ["https://t.me/c/123456/5"]
    client.get_messages.assert_awaited_once_with("cached", ids=5)


async def test_join_reports_admin_approval():
    client = AsyncMock(side_effect=errors.InviteRequestSentError(None))
    with pytest.raises(JoinPending):
        await TelegramMedia(client).join(JoinLink(invite_hash="abc"))
    assert isinstance(
        client.call_args.args[0], functions.messages.ImportChatInviteRequest
    )


async def test_comment_download_uses_linked_discussion_chat():
    channel = types.InputPeerChannel(123456, 1)
    discussion = types.InputPeerChannel(654321, 2)
    source = SimpleNamespace(peer_id=types.PeerChannel(123456))
    linked = SimpleNamespace(peer_id=types.PeerChannel(654321))
    client = AsyncMock(return_value=SimpleNamespace(messages=[linked, source]))
    client.get_input_entity = AsyncMock(side_effect=[channel, discussion])
    client.get_messages = AsyncMock(
        return_value=media_message(99, group_id=None)
    )
    group = await TelegramMedia(client).resolve(MessageLink("channel", 5, 99))
    assert group.urls == ["https://t.me/c/654321/99"]
    client.get_messages.assert_awaited_once_with(discussion, ids=99)


@pytest.mark.parametrize("target", ["1234567890", "-1001234567890"])
async def test_channel_targets_use_marked_channel_id(target):
    client = SimpleNamespace(get_input_entity=AsyncMock(return_value="channel"))
    await TelegramMedia(client).validate_target(Target(target))
    client.get_input_entity.assert_awaited_once_with(-1001234567890)
