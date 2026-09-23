import logging

import discord
from discord.ext import commands

import config

LOG = logging.getLogger("bot.welcome")


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member):
        channel = discord.utils.get(member.guild.channels, name=config.CHANNEL_WELCOME)
        if not channel:
            LOG.warning("Welcome channel %r not found", config.CHANNEL_WELCOME)
            return

        embed = discord.Embed(
            title="🎉 Welcome to the Robotics Club's Server! 🎉",
            description=f"Hey {member.mention}, we're happy to have you here!\n"
                        "Finish onboarding in your DMs to unlock the rest of the server!\n ",
            color=discord.Color.blue(),
        )
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
        embed.add_field(name="📜 Rules", value=f"Make sure to check out `#{config.CHANNEL_RULES}`", inline=False)
        embed.add_field(name="💬 Engage", value=f"Join discussions in `#{config.CHANNEL_MAIN}`", inline=False)
        embed.set_footer(
            text=f"Enjoy your stay! You're member #{member.guild.member_count} of the club.",
            icon_url="https://cdn-icons-png.flaticon.com/512/1035/1035670.png",
        )

        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Welcome(bot))