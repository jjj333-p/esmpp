"""
Python/XMPP partial port of https://github.com/esmBot/esmBot/
Copyright (C) 2026 Joseph Winkie

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import logging
import subprocess
import traceback
# from builtins import function
from collections.abc import Iterable

import json
import random

import aiohttp
import asyncio

import slixmpp
from slixmpp.types import PresenceArgs

from image_manipulation import EsmBotImageProcessor

with open("./login.json", encoding="utf-8") as lf:
    login = json.load(lf)

max_file_len: int = int(login.get("max_file_len", 10)) * \
                    1024 * 1024  # 5MB max file size


def get_git_info() -> tuple[str, str]:
    """
    Attempts to fetch the current Git version and remote origin URL.
    Returns (version, url) with safe fallbacks if Git is unavailable.
    """
    version = "unknown version"
    url = "https://github.com/jjj333-p/esmpp"

    try:
        version = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()

        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()

        # Optional: If you want to convert SSH URLs (git@...) to HTTPS for clickability
        if url.startswith("git@"):
            url = url.replace(":", "/").replace("git@", "https://")

    except Exception:
        # Fails silently if git is not installed or it's not a git repository
        pass

    return version, url


git_version, repo_url = get_git_info()

status_msg = f"esmpp v{git_version} | Source: {repo_url}"

aiohttp_session: aiohttp.ClientSession | None = None

acceptable_formats: list[str] = [
    "image/png",
    "image/jpg",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
    # "image/heic",
    # "image/heif"
]


async def fetch_media(url: str) -> tuple[bytes, str]:
    if aiohttp_session is None:
        raise RuntimeError("aiohttp session not available")

    async with aiohttp_session.get(url) as response:
        content_type = response.headers.get(
            'content-type', 'image/jpeg'
        )  # fallback to jpeg if not found
        if content_type not in acceptable_formats:
            raise ValueError(f"Unsupported content type: {content_type}")
        content_length = int(
            response.headers.get('content-length', '0')
        )
        if content_length > max_file_len:
            raise ValueError(f"File too large ({content_length} bytes > {max_file_len} bytes)")

        return await response.read(), content_type


def remove_fallbacks(body: str, fallbacks: Iterable[str]) -> str:
    # get out fallback ranges for features we support
    ranges_to_remove = []
    for fallback in fallbacks:
        if fallback["for"] == "urn:xmpp:reply:0":
            start = int(fallback["body"]["start"])
            end = int(fallback["body"]["end"])
            ranges_to_remove.append((start, end))
    # Remove in reverse order so indices stay valid
    ranges_to_remove.sort(reverse=True)
    result = list(body)
    for start, end in ranges_to_remove:
        if end > len(result):
            continue
        if start < 0:
            continue
        del result[start:end]

    return "".join(result).strip()


EIGHTBALL_RESPONSES = [
    "It is certain",
    "It is decidedly so",
    "Without a doubt",
    "Yes definitely",
    "You may rely on it",
    "As I see it, yes",
    "Most likely",
    "Outlook good",
    "Yes",
    "Signs point to yes",
    "Reply hazy, try again",
    "Ask again later",
    "Better not tell you now",
    "Cannot predict now",
    "Concentrate and ask again",
    "Don't count on it",
    "My reply is no",
    "My sources say no",
    "Outlook not so good",
    "Very doubtful",
]

PREFIX = ","

last_media: dict[str, str] = {}

vips = EsmBotImageProcessor()


def raise_for_insufficient_args(arr: list | None, n: int = 1):
    if arr is None or len(arr) < n:
        raise ValueError(f"Insufficient arguments: `{'`, `'.join(arr or [])}`")


class MUCBot(slixmpp.ClientXMPP):

    def __init__(self, jid, password, rooms, nick):
        slixmpp.ClientXMPP.__init__(self, jid, password)

        self.rooms = rooms
        self.nick = nick

        self.add_event_handler("session_start", self.start)

        # The groupchat_message event is triggered whenever a message
        # stanza is received from any chat room. If you also also
        # register a handler for the 'message' event, MUC messages
        # will be processed by both handlers.
        self.add_event_handler("message", self.message)

        self.add_event_handler("groupchat_direct_invite", self.invite)

        self.register_plugin('xep_0030')  # Service Discovery
        self.register_plugin('xep_0045')  # Multi-User Chat
        self.register_plugin('xep_0199')  # XMPP Ping
        self.register_plugin('xep_0461')  # Message Replies
        self.register_plugin('xep_0066')  # oob (media)
        self.register_plugin('xep_0359')  # (Unique and Stable Stanza IDs)
        self.register_plugin('xep_0249')  # muc invites
        self.register_plugin('xep_0363')  # upload component

    async def start(self, _):
        await self.get_roster()
        self.send_presence()

        # join configured rooms
        for room in self.rooms:
            await self.plugin['xep_0045'].join_muc_wait(
                room,
                self.nick,
                presence_options=PresenceArgs(
                    pstatus=status_msg
                ),
                maxchars=0,
            )

    async def invite(self, msg):
        """
        Handler triggered when a Direct MUC Invite (XEP-0249) is received.
        The plugin parses the XML and exposes it through message['xep_0249'].
        """

        # not invitable atm
        return

        # Extract the invite payload
        invite = msg['groupchat_invite']

        # The plugin gives you clean access to the underlying attributes
        room_jid = invite['jid']
        room_password = invite['password']
        # unsupported for now
        if room_password:
            return
        reason = invite['reason']

        sender = msg['from']
        print(f"Received a direct invite to {room_jid} from {sender}")

        if reason:
            print(f"Reason: {reason}")

        # Join the room immediately
        try:
            await self.plugin['xep_0045'].join_muc_wait(
                room_jid,
                self.nick,
                presence_options=PresenceArgs(
                    pstatus=status_msg
                ),
                maxchars=0,
            )
            print(f"Successfully joined {room_jid}")
        except Exception as e:
            print(f"Failed to join {room_jid}: {e}")
            return

        # Add room to login.json and save
        try:
            with open("./login.json", "r", encoding="utf-8") as lf:
                login_data = json.load(lf)

            if room_jid not in login_data.get("rooms", []):
                login_data.setdefault("rooms", []).append(room_jid)
                self.rooms.append(room_jid)

                with open("./login.json", "w", encoding="utf-8") as lf:
                    json.dump(login_data, lf, indent=2, ensure_ascii=False)

                print(f"Added {room_jid} to login.json")
        except Exception as e:
            print(f"Failed to update login.json: {e}")

        self.send_message(
            mto=room_jid,
            mbody=f"Joined muc as I was invited by `{sender}` with reason `{reason}` .",
            mtype="groupchat"
        )

    async def message(self, msg):
        # dont respond to self
        if msg['mucnick'] == self.nick:
            return

        replyto_jid = msg['from'].bare if \
            (msg['from'].bare in self.rooms and msg['type'] == "groupchat") or \
            (msg['from'].bare not in self.rooms and msg['type'] == "chat") \
            else msg['from']

        origin_id = msg.get('origin_id', {}).get('id')

        replyto_id = msg.get('stanza_id', {}).get('id', '') if msg['type'] == 'groupchat' else (
            origin_id if origin_id else msg['id']
        )

        if not msg["body"].startswith(PREFIX):
            # get attachment url
            attachment_url: str | None = msg.get('oob', {}).get('url')
            if attachment_url:
                last_media[msg['from'].bare] = attachment_url

            # ignore non-cmds
            return

        cmd_args: list[str] = msg['body'][len(PREFIX):].split()

        cmd = cmd_args.pop(0)

        if len(cmd_args) > 0 and cmd_args[0].startswith("https://"):
            url = cmd_args[0]
            cmd_args.pop(0)
        # TODO: check for reply
        else:
            url = last_media.get(msg['from'].bare)

        async def reply_bytes(i: bytes, extension: str = "webp", mimetype: str = "image/webp"):
            upload_url = await self.plugin['xep_0363'].upload_file(
                filename=f"generated.{extension}",
                # domain=self.domain,
                timeout=10,
                input_file=i,
                size=len(i),
                content_type=mimetype,
            )

            print(upload_url)
            last_media[msg['from'].bare] = upload_url

            message: slixmpp.stanza.Message = self.plugin['xep_0461'].make_reply(
                msg['from'],
                replyto_id,
                f"> {msg['body']}",
                mto=replyto_jid,
                mbody=upload_url,
                mtype=msg['type']
            )
            # attach media tag
            # pylint: disable=invalid-sequence-index
            message['oob']['url'] = upload_url
            message.send()

        async def process_img(func, *args):
            if url is None:
                raise ValueError("No recently sent media url")

            b, content_type = await fetch_media(url)

            i = await func(b, *args)

            await reply_bytes(i)

        print("processing cmd", cmd, "for", msg['from'])

        try:
            match cmd:
                case "invert":
                    await process_img(vips.invert)
                case "blur":
                    await process_img(vips.blur)
                case "sharpen":
                    await process_img(vips.sharpen)
                case "bounce":
                    await process_img(vips.bounce)
                case "caption":
                    await process_img(vips.caption, " ".join(cmd_args) or " ")
                case "caption2":
                    await process_img(vips.caption2, " ".join(cmd_args) or " ")
                case "shake":
                    await process_img(vips.circle)
                case "colors":
                    raise_for_insufficient_args(cmd_args, 2)
                    await process_img(vips.colors, cmd_args[0], int(cmd_args[1]))
                case "square":
                    await process_img(vips.crop)
                case "deepfry":
                    await process_img(vips.deepfry)
                case "fade":
                    await process_img(vips.fade, True)
                case "overlay":
                    raise_for_insufficient_args(cmd_args)
                    await process_img(vips.flag, cmd_args[0])
                case "flip":
                    await process_img(vips.flip, False)
                case "flop":
                    await process_img(vips.flip, True)
                case "gamexplain":
                    await process_img(vips.gamexplain)
                case "globe":
                    await process_img(vips.globe, False)
                case "snowglobe":
                    await process_img(vips.globe, True)
                case "jpeg":
                    if len(cmd_args) == 0:
                        await process_img(vips.jpeg)
                    else:
                        await process_img(vips.jpeg, int(cmd_args[0]))
                case "mirror":
                    await process_img(vips.mirror, False, False)
                case "mirror2":
                    await process_img(vips.mirror, False, True)
                case "mirror3":
                    await process_img(vips.mirror, True, False)
                case "mirror4":
                    await process_img(vips.mirror, True, True)
                case "reddit":
                    await process_img(vips.reddit, cmd_args[0])
                case "reverse":
                    await process_img(vips.reverse, False)
                case "soos":
                    await process_img(vips.reverse, True)
                case "scott":
                    await process_img(vips.scott)
                case "slide":
                    await process_img(vips.slide, False)
                case "slide2":
                    await process_img(vips.slide, True)
                case "homebrew":
                    raise_for_insufficient_args(cmd_args)
                    i = await vips.homebrew(" ".join(cmd_args))
                    await reply_bytes(i)
                case "sonic":
                    raise_for_insufficient_args(cmd_args)
                    i = await vips.sonic(" ".join(cmd_args))
                    await reply_bytes(i)
                case "create-qr":
                    raise_for_insufficient_args(cmd_args)
                    i = await vips.qr_create(" ".join(cmd_args))
                    await reply_bytes(i, "png", "image/png")
                case "read-qr":
                    if url is None:
                        raise Exception("No")
                    qr, _ = await fetch_media(url)
                    text = await vips.qr_read(qr)
                    self.plugin['xep_0461'].make_reply(
                        msg['from'],
                        replyto_id,
                        f"> {msg['body']}",
                        mto=replyto_jid,
                        mbody=f"QR Text:\n```\n{text}\n```",
                        mtype=msg['type']
                    ).send()
                case "slow":
                    if len(cmd_args) > 0:
                        await process_img(vips.speed, True, int(cmd_args[0]))
                    else:
                        await process_img(vips.speed, True)
                case "speed":
                    if len(cmd_args) > 0:
                        await process_img(vips.speed, False, int(cmd_args[0]))
                    else:
                        await process_img(vips.speed, False)
                case "spin":
                    angle = int(cmd_args[0]) if len(cmd_args) > 0 else 0
                    await process_img(vips.spin, angle)
                case "squish":
                    await process_img(vips.squish)
                case "swirl":
                    await process_img(vips.swirl)
                case "tile":
                    await process_img(vips.tile)
                case "wall":
                    await process_img(vips.wall)
                case "whisper":
                    raise_for_insufficient_args(cmd_args)
                    await process_img(vips.whisper, " ".join(cmd_args))
                case "8ball":
                    self.plugin['xep_0461'].make_reply(
                        msg['from'],
                        replyto_id,
                        f"> {msg['body']}",
                        mto=replyto_jid,
                        mbody=random.choice(EIGHTBALL_RESPONSES),
                        mtype=msg['type']
                    ).send()
                case _:
                    self.plugin['xep_0461'].make_reply(
                        msg['from'],
                        replyto_id,
                        f"> {msg['body']}",
                        mto=replyto_jid,
                        mbody=f"Unknown command `{cmd}`",
                        mtype=msg['type']
                    ).send()
        except Exception as e:
            tb_str = f"{str(e)}\n{"".join(traceback.format_exception(e))}"
            print(tb_str)
            self.plugin['xep_0461'].make_reply(
                msg['from'],
                replyto_id,
                f"> {msg['body']}",
                mto=replyto_jid,
                mbody=f"Exception occurred executing your command: \n```\n{tb_str}\n```",
                mtype=msg['type']
            ).send()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(levelname)-8s %(message)s')

    xmpp = MUCBot(login["jid"], login["password"],
                  login["rooms"], login["displayname"])

    aiohttp_session = aiohttp.ClientSession(loop=asyncio.get_event_loop())

    # Connect to the XMPP server and start processing XMPP stanzas.
    xmpp.connect()
    print("Connected and running forever...")
    asyncio.get_event_loop().run_forever()
