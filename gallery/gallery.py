import asyncio
from datetime import datetime
import discord
import logging
import os
import re
from time import time
from discord.ext import commands
from cogs.utils.dataIO import dataIO
from cogs.utils.chat_formatting import error

__version__ = '1.3.0'

logger = logging.getLogger("red.gallery")

PATH = 'data/gallery'
JSON = PATH + 'settings.json'

POLL_INTERVAL = 5 * 60  # 5 minutes

UNIT_TABLE = (
    (('weeks', 'wks', 'w'), 60 * 60 * 24 * 7),
    (('days', 'dys', 'd'), 60 * 60 * 24),
    (('hours', 'hrs', 'h'), 60 * 60),
    (('minutes', 'mins', 'm'), 60),
    (('seconds', 'secs', 's'), 1),
)

DEFAULTS = {
    'ENABLED'     : False,
    'ARTIST_ROLE' : 'artist',
    'EXPIRATION'  : 60 * 60 * 24 * 2,  # 2 days
    'PIN_EMOTES'  : ['\N{ARTIST PALETTE}', '\N{PUSHPIN}'],
    'PRIV_ONLY'   : False
}

RM_EMOTES = ['❌']



# Exceptions
class CleanupError(Exception):
    def __init__(self, channel, orig):
        self.channel = channel
        self.original = orig


class BadTimeExpr(ValueError):
    pass


def _find_unit(unit):
    for names, length in UNIT_TABLE:
        if any(n.startswith(unit) for n in names):
            return names, length
    raise BadTimeExpr("Invalid unit: %s" % unit)


def _parse_time(time):
    time = time.lower()
    if not time.isdigit():
        time = re.split(r'\s*([\d.]+\s*[^\d\s,;]*)(?:[,;\s]|and)*', time)
        time = sum(map(_timespec_sec, filter(None, time)))
    return int(time)


def _timespec_sec(expr):
    atoms = re.split(r'([\d.]+)\s*([^\d\s]*)', expr)
    atoms = list(filter(None, atoms))

    if len(atoms) > 2:  # This shouldn't ever happen
        raise BadTimeExpr("invalid expression: '%s'" % expr)
    elif len(atoms) == 2:
        names, length = _find_unit(atoms[1])
        if atoms[0].count('.') > 1 or \
                not atoms[0].replace('.', '').isdigit():
            raise BadTimeExpr("Not a number: '%s'" % atoms[0])
    else:
        names, length = _find_unit('seconds')

    return float(atoms[0]) * length


def _generate_timespec(sec, short=False, micro=False):
    timespec = []

    for names, length in UNIT_TABLE:
        n, sec = divmod(sec, length)

        if n:
            if micro:
                s = '%d%s' % (n, names[2])
            elif short:
                s = '%d%s' % (n, names[1])
            else:
                s = '%d %s' % (n, names[0])
            if n <= 1:
                s = s.rstrip('s')
            timespec.append(s)

    if len(timespec) > 1:
        if micro:
            return ''.join(timespec)

        segments = timespec[:-1], timespec[-1:]
        return ' and '.join(', '.join(x) for x in segments)

    return timespec[0]


class Gallery:
    """Message auto-deletion for gallery channels"""
    def __init__(self, bot):
        self.bot = bot
        self.settings = dataIO.load_json(JSON)
        self.task = bot.loop.create_task(self.loop_task())


    def __unload(self):
        self.task.cancel()

    def save(self):
        dataIO.save_json(JSON, self.settings)

    def settings_for(self, channel: discord.Channel) -> dict:
        cid = channel.id
        if cid not in self.settings:
            return DEFAULTS
        return self.settings[cid]

    def update_setting(self, channel: discord.Channel, key: str, val) -> None:
        cid = channel.id
        if cid not in self.settings:
            self.settings[cid] = DEFAULTS
        self.settings[cid][key] = val
        self.save()

    def enabled_in(self, chan: discord.Channel) -> bool:
        return chan.id in self.settings and self.settings[chan.id]['ENABLED']

    async def message_check(self, message: discord.Message) -> bool:
        assert self.enabled_in(message.channel)

        server = message.server
        author = message.author
        settings = self.settings_for(message.channel)
        priv_only = settings.get('PRIV_ONLY', False)

        mod_role = self.bot.settings.get_server_mod(server).lower()
        admin_role = self.bot.settings.get_server_admin(server).lower()
        artist_role = settings['ARTIST_ROLE'].lower()
        priv_roles = [mod_role, admin_role]
        privileged = False
        if isinstance(author, discord.Member):
            privileged = any(r.name.lower() in priv_roles + [artist_role]
                             for r in author.roles)

        message_age = (datetime.utcnow() - message.timestamp).total_seconds()
        expired = message_age > settings['EXPIRATION']

        attachment = bool(message.attachments) or bool(message.embeds)

        e_pin = any(e in message.content for e in settings['PIN_EMOTES'])
        r_pin = False
        x_pin = False
        for reaction in message.reactions:
            if reaction.emoji not in settings['PIN_EMOTES'] + RM_EMOTES:
                continue
            users = await self.bot.get_reaction_users(reaction)
            for user in users:
                member = server.get_member(user.id)
                if not member:
                    continue
                if reaction.emoji in RM_EMOTES and not x_pin:
                    x_pin |= any(r.name.lower() in priv_roles
                                 for r in member.roles)
                elif not r_pin:
                    r_pin |= any(r.name.lower() in priv_roles + [artist_role]
                                 for r in member.roles)
        pinned = r_pin or message.pinned or (e_pin and privileged)
        keep = pinned or attachment and not (priv_only and not privileged)
        return expired and (x_pin or not keep)

    async def cleanup_task(self, channel: discord.Channel) -> None:
        try:
            to_delete = []
            async for message in self.bot.logs_from(channel, limit=2000):
                if await self.message_check(message):
                    to_delete.append(message)

            await self.mass_purge(to_delete)
        except Exception as e:
            raise CleanupError(channel, e)

    async def loop_task(self):
        await self.bot.wait_until_ready()
        try:
            while True:
                start = time()

                tasks = []
                for cid, d in self.settings.items():
                    if not d['ENABLED']:
                        continue
                    channel = self.bot.get_channel(cid)
                    if not channel:
                        logger.warning('Attempted to curate missing channel '
                                       'ID #%s, disabling.' % cid)
                        d['ENABLED'] = False
                        self.save()
                        continue
                    tasks.append(self.cleanup_task(channel))

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, CleanupError):
                        logger.exception("Exception cleaning in %s #%s:"
                                         % (res.channel.server, res.channel),
                                         exc_info=res.original)
                elapsed = time() - start
                await asyncio.sleep(POLL_INTERVAL - elapsed)
        except asyncio.CancelledError:
            pass

    @commands.group(pass_context=True, allow_dm=False)
    @checks.mod_or_permissions(manage_messages=True)
    async def galset(self, ctx):
        """Gallery module settings"""
        if ctx.invoked_subcommand is None:
            await self.bot.send_cmd_help(ctx)

    @galset.command(pass_context=True, allow_dm=False)
    async def emotes(self, ctx, *emotes):
        """Show or update the emotes used to indicate artwork"""
        channel = ctx.message.channel
        if not emotes:
            em = self.settings_for(channel)['PIN_EMOTES']
            await self.bot.say('Pin emotes for this channel: ' + ' '.join(em))
        else:
            if any(len(x) != 1 for x in emotes):
                await self.bot.say('Error: You can only use unicode emotes.')
                return
            self.update_setting(channel, 'PIN_EMOTES', emotes)
            await self.bot.say('Updated pin emotes for this channel.')

    @galset.command(pass_context=True, allow_dm=False)
    async def turn(self, ctx, on_off: bool = None):
        """Turn gallery message curation on or off"""
        channel = ctx.message.channel
        current = self.settings_for(channel)['ENABLED']
        perms = channel.permissions_for(channel.server.me).manage_messages
        adj_bool = current if on_off is None else on_off
        adj = 'enabled' if adj_bool else 'disabled'
        if on_off is None:
            await self.bot.say('Gallery cog is %s in this channel.' % adj)
        else:
            if self.enabled_in(channel) == on_off:
                await self.bot.say('Already %s.' % adj)
            else:
                if on_off and not perms:
                    await self.bot.say('I need the "Manage messages" '
                                       'permission in this channel to work.')
                    return
                self.update_setting(channel, 'ENABLED', on_off)
                await self.bot.say('Gallery curation %s.' % adj)

    @galset.command(pass_context=True, allow_dm=False)
    async def privonly(self, ctx, on_off: bool = None):
        """Set whether only privileged users' messages are kept

        If disabled (default), all attachments and embeds are kept."""
        channel = ctx.message.channel
        adj = 'enabled' if on_off else 'disabled'
        priv_only = self.settings_for(channel).get('PRIV_ONLY', False)
        if on_off == priv_only:
            await self.bot.say('Privileged-only posts already %s.' % adj)
        else:
            self.update_setting(channel, 'PRIV_ONLY', on_off)
            await self.bot.say('Privileged-only posts %s.' % adj)

    @galset.command(pass_context=True, allow_dm=False)
    async def age(self, ctx, *, timespec: str = None):
        """Set the maximum age of non-art posts"""
        channel = ctx.message.channel
        if not timespec:
            sec = self.settings_for(channel)['EXPIRATION']
            await self.bot.say('Current maximum age is %s.'
                               % _generate_timespec(sec))
        else:
            try:
                sec = _parse_time(timespec)
            except BadTimeExpr as e:
                await self.bot.say(error(e.args[0]))
                return

            self.update_setting(channel, 'EXPIRATION', sec)
            await self.bot.say('Maximum post age set to %s.' %
                               _generate_timespec(sec))

    @galset.command(pass_context=True, allow_dm=False)
    async def role(self, ctx, role: discord.Role = None):
        """Sets the artist role"""
        channel = ctx.message.channel
        if role is None:
            role = self.settings_for(channel)['ARTIST_ROLE']
            await self.bot.say('Artist role is currently %s' % role)
        else:
            self.update_setting(channel, 'ARTIST_ROLE', role.name)
            await self.bot.say('Artist role set.')

    # Stolen from mod.py
    async def mass_purge(self, messages):
        while messages:
            if len(messages) > 1:
                await self.bot.delete_messages(messages[:100])
                messages = messages[100:]
            else:
                await self.bot.delete_message(messages[0])
                messages = []
            await asyncio.sleep(1)


def check_folders():
    if not os.path.exists(PATH):
        print("Creating %s folder..." % PATH)
        os.makedirs(PATH)


def check_files():
    if not dataIO.is_valid_json(JSON):
        print("Creating empty %s" % JSON)
        dataIO.save_json(JSON, {})


def setup(bot):
    check_folders()
    check_files()
    bot.add_cog(Gallery(bot))
