from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
from pathlib import Path
import logging
from typing import Optional
import aiohttp
import httpx
import discord
from discord import app_commands
from dotenv import load_dotenv
import sqlite3
from pluralkit import Client as PKClient, PluralKitException, SystemNotFound
from discord.ext import tasks

#yes. this is just one python file. im lazy :p

#consts/envs
load_dotenv()
def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"missing required environment variable {name!r}. "
        )
    return value


DISCORD_TOKEN = _require("DISCORD_TOKEN")
APPLICATION_ID = _require("DISCORD_APPLICATION_ID")
OAUTH_AUTHORIZE_URL = _require("OAUTH_AUTHORIZE_URL")

DISCORD_API = "https://discord.com/api/v9"

#logging stuffs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pk_fronters_widget")

class PluralKitError(RuntimeError):
    pass
class WidgetError(RuntimeError):
    pass

#database stuffs
DB_PATH = Path(os.environ.get("DB_PATH") or Path(__file__).with_name("pk_fronters_widget.db"))
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute(
    "create table if not exists links (user_id integer primary key, linked_at text not null, last_update text, last_fronts text, identity_id integer)"
)
_db.commit()

def get_identity_id(discord_id: int) -> Optional[int]:
    cur = _db.execute("select identity_id from links where user_id = ?", (discord_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None

def generate_identity_id() -> int:
    while True:
        id = secrets.randbits(63)
        if id and not _db.execute(
            "select 1 from links where identity_id = ?", (id,)
        ).fetchone():
            return id

def _migrate_identity_ids() -> None:
    cols = [r[1] for r in _db.execute("pragma table_info(links)")]
    if "identity_id" not in cols:
        _db.execute("alter table links add column identity_id integer")
        _db.commit()
    for (uid,) in _db.execute("select user_id from links where identity_id is null").fetchall():
        _db.execute("update links set identity_id = ? where user_id = ?", (generate_identity_id(), uid))
    _db.execute("create unique index if not exists idx_links_identity on links(identity_id)")
    _db.commit()

_migrate_identity_ids()

def link(discord_id: int):
    now = datetime.now(timezone.utc).isoformat()
    identity_id = get_identity_id(discord_id) or generate_identity_id()
    _db.execute(
        "insert or replace into links (user_id, linked_at, last_update, last_fronts, identity_id) values (?, ?, ?, ?, ?)",
        (discord_id, now, now, "", identity_id),
    )
    _db.commit()
    _system_cache.pop(discord_id, None)


def is_linked(discord_id: int):
    cur = _db.execute("select 1 from links where user_id = ?", (discord_id,))
    return cur.fetchone() is not None

def mark_update(discord_id: int, fronts: str):
    now = datetime.now(timezone.utc).isoformat()
    _db.execute(
        "update links set last_update = ?, last_fronts = ? where user_id = ?",
        (now, fronts, discord_id),
    )
    _db.commit()

def touch_update(discord_id: int):
    now = datetime.now(timezone.utc).isoformat()
    _db.execute(
        "update links set last_update = ? where user_id = ?",
        (now, discord_id),
    )
    _db.commit()

def get_due_users(stale_after_seconds: int, limit: int) -> list[tuple[int, str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
    rows = _db.execute(
        "select user_id, last_fronts from links where last_update < ? "
        "order by last_update asc limit ?",
        (cutoff, limit),
    ).fetchall()
    return [(row[0], row[1] or "") for row in rows]

#pk api stuffs
_pk = PKClient(user_agent="pk_fronters_widget/0.1.0 (+https://github.com/asleepyskye/pk_fronters_widget)")

@dataclass
class Fronter:
    name: str
    pkid: str
    pronouns: Optional[str]
    avatar_url: Optional[str]

@dataclass
class SystemInfo:
    name: Optional[str]
    id: Optional[str]
    pronouns: Optional[str]
    avatar_url: Optional[str]

@dataclass
class FrontStatus:
    system_name: Optional[str]
    system_id: Optional[str]
    system_pronouns: Optional[str]
    system_avatar_url: Optional[str]
    fronters: list[Fronter]

SYSTEM_CACHE_TTL = timedelta(hours=1)
_system_cache: dict[int, tuple[datetime, SystemInfo]] = {}

async def get_system_cached(discord_id: int) -> SystemInfo:
    now = datetime.now(timezone.utc)
    cached = _system_cache.get(discord_id)
    if cached and now - cached[0] < SYSTEM_CACHE_TTL:
        return cached[1]
    system = await _pk.get_system(discord_id)
    info = SystemInfo(
        name=system.name,
        id=system.id.id.replace("-", ""),
        pronouns=system.pronouns,
        avatar_url=system.avatar_url,
    )
    _system_cache[discord_id] = (now, info)
    return info

async def fetch_front(discord_id: int) -> FrontStatus:
    try:
        system = await get_system_cached(discord_id)
        members = [m async for m in _pk.get_fronters(discord_id)]
    except SystemNotFound:
        raise PluralKitError(
            "no PluralKit system is linked to your Discord account."
        )
    except PluralKitException as e:
        log.error(e);
        raise PluralKitError(
            f"PluralKit didn't return fronters."
        )
    except httpx.HTTPError as e:
        log.error(f"PluralKit request failed: {e!r}")
        raise PluralKitError(
            "PluralKit request failed (timeout or network error)."
        )

    fronters = [
        Fronter(
            name=m.display_name or m.name,
            pkid = m.id.id.replace("-", ""),
            pronouns=m.pronouns,
            avatar_url=m.avatar_url,
        )
        for m in members
    ]
    return FrontStatus(
        system_name=system.name,
        system_id=system.id,
        system_pronouns=system.pronouns,
        system_avatar_url=system.avatar_url,
        fronters=fronters,
    )

def fronts_string(status: FrontStatus) -> str:
    return ",".join(f.pkid for f in status.fronters)

# widget stuffs
WIDGET_T_STRING = 1
WIDGET_T_NUMBER = 2
WIDGET_T_MEDIA = 3

#uhhh this is probably a bad idea. i dont think this'll change tho?
MORE_URL = "https://cdn.discordapp.com/app-assets/1511072634232770621/1511108933371297873.png?size=1024"

_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

def _clean(text: Optional[str]):
    text = _LINK_RE.sub(r"\1", text or "")
    return text

def _trunc(text: Optional[str]):
    if len(text) > 21:
        text = text[:20] + "…"
    return text

def _string(name: str, value: Optional[str]) -> dict:
    return {"type": WIDGET_T_STRING, "name": name, "value": value or ""}

def _media(name: str, url: Optional[str]) -> Optional[dict]:
    if not url:
        return None
    return {"type": WIDGET_T_MEDIA, "name": name, "value": {"url": url}}


def build_data(status: FrontStatus) -> list[dict]:
    fronting_str = "Currently switched out."
    if len(status.fronters) > 0:
        fronting_str = f"{len(status.fronters)} currently fronting"
    data: list[dict] = [
        _string("num_fronting_str", fronting_str),
        _string("system_name", _clean(status.system_name)),
        _string("system_id", status.system_id),
        _string("system_pronouns", _clean(status.system_pronouns)),
    ]
    if img := _media("system_img", status.system_avatar_url):
        data.append(img)

    for slot in range(1, 5):
        if slot == 4 and len(status.fronters) > 4:
            others = len(status.fronters) - 3
            data.append(_string("fronter4_name", f"{others} other fronters..."))
            data.append(_string("fronter4_field", None))
            data.append(_media(f"fronter{slot}_img", MORE_URL))
            continue

        fronter = status.fronters[slot - 1] if slot <= len(status.fronters) else None
        data.append(_string(f"fronter{slot}_name", _trunc(_clean(fronter.name)) if fronter else None))
        data.append(_string(f"fronter{slot}_field", _trunc(_clean(fronter.pronouns)) if fronter else None))
        if fronter and (img := _media(f"fronter{slot}_img", fronter.avatar_url or status.system_avatar_url)):
            data.append(img)

    return data


async def push_profile(discord_user_id: int, status: FrontStatus) -> None:
    identity_id = get_identity_id(discord_user_id)
    if identity_id is None:
        raise WidgetError("no identity id assigned to this user")
    payload = {
        "username": status.system_id,
        "data": {"dynamic": build_data(status)},
    }
    url = (
        f"{DISCORD_API}/applications/{APPLICATION_ID}"
        f"/users/{discord_user_id}/identities/{identity_id}/profile"
    )
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.patch(url, json=payload, headers=headers) as resp:
            if resp.status >= 400:
                log.error(await resp.text())
                raise WidgetError(f"discord API error {resp.status}")
            
#background polling
POLL_INTERVAL = 10
STALE_AFTER = 30
LIMIT = 5

@tasks.loop(seconds=POLL_INTERVAL)
async def poll_fronts() -> None:
    for user_id, last_fronts in get_due_users(STALE_AFTER, LIMIT):
        try:
            status = await fetch_front(user_id)

            fronts = fronts_string(status)
            if fronts == last_fronts:
                touch_update(user_id)
                continue

            log.info(f"updating widget for user {user_id}")
            await push_profile(user_id, status)
            mark_update(user_id, fronts)
        except (PluralKitError, WidgetError) as e:
            log.error(e)
            touch_update(user_id)
        except Exception:
            log.exception(f"unexpected error updating user {user_id}")
            touch_update(user_id)

@poll_fronts.before_loop
async def _before_poll_fronts() -> None:
    await _bot.wait_until_ready()

#actual bot stuffs
class WidgetBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        self.tree.add_command(widget)
        await self.tree.sync()
        log.info("command tree synced.")
        poll_fronts.start()


widget = app_commands.Group(name="widget", description="manage the PK fronter widget")
widget.allowed_installs = app_commands.AppInstallationType(guild=False, user=True)
widget.allowed_contexts = app_commands.AppCommandContext(
    guild=True, dm_channel=True, private_channel=True
)

def _authorize_view() -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.link,
            label="Authorize",
            url=OAUTH_AUTHORIZE_URL,
        )
    )
    return view

@widget.command(name="setup", description="setup the fronter widget")
async def setup(interaction: discord.Interaction):
    try:
        status = await fetch_front(interaction.user.id)
    except PluralKitError as e:
        await interaction.response.send_message(e, ephemeral=True)
        return

    link(interaction.user.id)

    await interaction.response.send_message(
        f"found system **{status.system_name or status.system_id}**!\n\n"
        "click **Authorize** below to continue :3 \n"
        "after authorizing, you can dismiss this message and add the widget in your profile!",
        view=_authorize_view(),
        ephemeral=True,
    )

@widget.command(name="refresh", description="force-refresh your widget")
async def refresh(interaction: discord.Interaction):
    if not is_linked(interaction.user.id):
        await interaction.response.send_message(
            "you haven't set up the widget yet, run `/widget setup` first.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    _system_cache.pop(interaction.user.id, None)
    try:
        status = await fetch_front(interaction.user.id)
        await push_profile(interaction.user.id, status)
    except (PluralKitError, WidgetError) as e:
        await interaction.followup.send(f"{e}", ephemeral=True)
        return

    mark_update(interaction.user.id, fronts_string(status))
    await interaction.followup.send("refreshed!", ephemeral=True)

_bot = WidgetBot()

def main() -> None:
    _bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
