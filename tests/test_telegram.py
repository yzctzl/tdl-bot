"""Media-group lookup, original captions and explicit join requests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors
from telethon.tl import functions, types

from tdl_bot.core.errors import AccessDenied, JoinPending, UserError
from tdl_bot.core.links import JoinLink, MessageLink
from tdl_bot.integrations.telegram.media import TelegramMedia
from tdl_bot.storage.models import Target

pytestmark = pytest.mark.usefixtures("chinese")


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
        upload_file = AsyncMock(return_value=types.InputFile(1, 1, "x", ""))
        send_file = AsyncMock(return_value=[1, 2, 3])

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
    await media.upload(
        group, [Path("one"), Path("two"), Path("three")], Target("me", 77)
    )
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
    await media.upload(group, [Path("video")], Target("me"))
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


async def test_server_copy_drops_author_preserves_album_and_topic():
    client = AsyncMock(
        return_value=types.Updates(
            updates=[
                types.UpdateNewChannelMessage(
                    types.Message(
                        id=i,
                        peer_id=types.PeerChannel(123),
                        date=None,
                        message="text",
                    ),
                    pts=1,
                    pts_count=1,
                )
                for i in [200, 201]
            ],
            users=[],
            chats=[],
            date=None,
            seq=1,
        )
    )
    client.get_input_entity.return_value = types.InputPeerChannel(123, 1)
    client.get_messages.return_value = [media_message(1), media_message(2)]
    copied = await TelegramMedia(client).copy_messages(
        "channel", [1, 2], Target("other", 77)
    )
    request = client.call_args.args[0]
    assert request.id == [1, 2]
    assert request.drop_author is True
    assert request.drop_media_captions is None
    assert request.top_msg_id == 77
    assert copied == [200, 201]


async def test_missing_cached_album_member_never_partially_forwards():
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChannel(123, 1)
    client.get_messages.return_value = [media_message(1), None]
    assert (
        await TelegramMedia(client).copy_messages(
            "channel", [1, 2], Target("other")
        )
        is None
    )
    client.assert_not_awaited()


async def test_protected_source_falls_back_only_for_forward_restriction():
    media = TelegramMedia(AsyncMock())
    media.copy_messages = AsyncMock(
        side_effect=errors.ChatForwardsRestrictedError(None)
    )
    group = SimpleNamespace(
        link=MessageLink("channel", 1), messages=[media_message(1)]
    )
    assert await media.copy_group(group, Target("other")) is None
    media.copy_messages.side_effect = errors.ChatWriteForbiddenError(None)
    with pytest.raises(errors.ChatWriteForbiddenError):
        await media.copy_group(group, Target("other"))


@pytest.mark.parametrize(
    "entity",
    [
        types.Channel(
            id=123,
            title="channel",
            photo=types.ChatPhotoEmpty(),
            date=None,
        ),
        types.User(id=123, bot=True),
    ],
)
async def test_user_resolution_rejects_channels_and_bots(entity):
    client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))
    with pytest.raises(UserError, match="真实用户"):
        await TelegramMedia(client).resolve_user("@example")


@pytest.mark.parametrize("kind", ["photo", "video"])
async def test_reveal_removes_media_and_text_spoilers_only_on_storage_copy(
    kind,
):
    photo = types.Photo(
        id=90,
        access_hash=2,
        file_reference=b"ref",
        date=None,
        sizes=[],
        dc_id=2,
    )
    bold = types.MessageEntityBold(0, 4)
    message = SimpleNamespace(
        id=100,
        message="text",
        entities=[bold, types.MessageEntitySpoiler(0, 4)],
        photo=photo if kind == "photo" else None,
        document=None
        if kind == "photo"
        else types.Document(
            id=90,
            access_hash=2,
            file_reference=b"ref",
            date=None,
            mime_type="video/mp4",
            size=100,
            dc_id=2,
            attributes=[],
        ),
        media=SimpleNamespace(spoiler=True),
    )
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChannel(123, 1)
    client.get_messages.return_value = [message]
    media = TelegramMedia(client)
    assert await media.reveal_messages("-1000000000123", [100])
    request = client.call_args.args[0]
    assert isinstance(request, functions.messages.EditMessageRequest)
    assert request.id == 100
    assert request.peer.channel_id == 123
    assert request.media.id.id == 90
    assert request.media.spoiler is False
    assert request.message == "text"
    assert request.entities == [bold]
    # Already visible storage messages require no further edits.
    message.entities = [bold]
    message.media.spoiler = False
    client.reset_mock()
    assert await media.reveal_messages("-1000000000123", [100])
    client.assert_not_awaited()


async def test_upload_never_preserves_text_or_photo_spoilers():
    message = media_message(
        1,
        group_id=None,
        text="caption",
        entities=[
            types.MessageEntitySpoiler(0, 7),
            types.MessageEntityBold(0, 7),
        ],
    )
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="peer"),
        get_messages=AsyncMock(return_value=message),
        upload_file=AsyncMock(return_value=types.InputFile(1, 1, "x", "")),
        send_file=AsyncMock(return_value=SimpleNamespace(id=100)),
    )
    media = TelegramMedia(client)
    group = await media.resolve(MessageLink("channel", 1))
    assert await media.upload(group, [Path("photo")], Target("storage")) == [
        100
    ]
    assert client.send_file.call_args.args[1].spoiler is False
    entities = client.send_file.call_args.kwargs["formatting_entities"]
    assert len(entities) == 1 and isinstance(
        entities[0], types.MessageEntityBold
    )
