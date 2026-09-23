import logging

import discord
from discord.ext import commands

import config

LOG = logging.getLogger("bot.goodbye")


class Goodbye(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        channel = discord.utils.get(member.guild.channels, name=config.CHANNEL_GOODBYE)
        if not channel:
            LOG.warning("Goodbye channel %r not found", config.CHANNEL_GOODBYE)
            return

        embed = discord.Embed(
            title="😩 Goodbye, Friend!",
            description=f"{member.mention} has left the server.\n ",
            color=discord.Color.red(),
        )

        avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
        embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="🌍 Stay Connected!", value="Come back anytime! You're always welcome. 🤗", inline=False)
        embed.add_field(name="📌 Remember:", value="We had a great time together! 🥰", inline=False)
        embed.set_footer(text="Goodbye! Hope to see you again!", icon_url="https://cdn-icons-png.flaticon.com/512/1035/1035670.png")

        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Goodbye(bot))