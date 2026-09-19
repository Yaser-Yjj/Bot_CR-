"""Keyless public-API commands for Bot_CR.

Picks from openpublicapis.com that are genuinely useful for a robotics-club
Discord: Open-Meteo weather, the Free Dictionary, memes, CoinGecko prices,
SpaceX launches, GitHub profiles, advice slips, and LRCLIB lyrics. Every
endpoint is used with no API key, a friendly User-Agent, and a 10 s timeout.
"""

import logging

import aiohttp
import discord
from discord.ext import commands

LOG = logging.getLogger("bot.apis")

USER_AGENT = "Bot_CR/1.0 (robotics-club Discord bot; contact: server staff)"

WMO_CODES = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Fog", "🌫️"),
    48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Dense freezing drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Heavy freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy snow", "🌨️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"),
    81: ("Showers", "🌧️"),
    82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"),
    86: ("Heavy snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with hail", "⛈️"),
    99: ("Severe thunderstorm with hail", "⛈️"),
}

# Common ticker names -> CoinGecko ids.
COIN_ALIASES = {
    "btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana", "xrp": "ripple", "ripple": "ripple",
    "ada": "cardano", "cardano": "cardano", "doge": "dogecoin", "dogecoin": "dogecoin",
    "dot": "polkadot", "polkadot": "polkadot", "matic": "matic-network",
    "polygon": "matic-network", "link": "chainlink", "chainlink": "chainlink",
    "avax": "avalanche-2", "avalanche": "avalanche-2", "bnb": "binancecoin",
    "ton": "the-open-network", "apt": "aptos", "aptos": "aptos",
}


class Apis(commands.Cog):
    """Weather, dictionary, memes, crypto, SpaceX, GitHub & lyrics."""

    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    async def _session_get(self, url: str, *, params: dict | None = None) -> dict | None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": USER_AGENT},
            )
        async with self._session.get(url, params=params) as resp:
            if resp.status >= 400:
                LOG.warning("API %s -> HTTP %s", url, resp.status)
                return None
            return await resp.json(content_type=None)

    async def cog_unload(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ── weather (Open-Meteo) ───────────────────────────────────
    @commands.hybrid_command(name="weather", description="Current conditions for a city.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def weather(self, ctx, city: str):
        """Live conditions from Open-Meteo (no key needed)."""
        async with ctx.typing():
            geo = await self._session_get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
            )
            hits = (geo or {}).get("results") or []
            if not hits:
                await ctx.send(f"🌐 Couldn't find a city named **{city}**.")
                return
            place = hits[0]
            label = f"{place.get('name')}, {place.get('country')}"
            if place.get("admin1"):
                label = f"{place.get('name')}, {place.get('admin1')}, {place.get('country')}"
            data = await self._session_get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"], "longitude": place["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                    "timezone": "auto",
                },
            )
            current = (data or {}).get("current") or {}
            if not current:
                await ctx.send("⚠️ The weather API didn't answer. Try again in a bit.")
                return
            code = int(current.get("weather_code", 0))
            cond, icon = WMO_CODES.get(code, ("Unknown", "🌡️"))
            temp = current["temperature_2m"]
            feels = current["apparent_temperature"]
            humid = current["relative_humidity_2m"]
            wind = current["wind_speed_10m"]
        embed = discord.Embed(
            title=f"{icon} {cond} in {label}",
            color=discord.Color.blue(),
            description=(
                f"**{temp}°C** (feels like {feels}°C)\n"
                f"💧 Humidity {humid}% · 💨 Wind {wind} km/h"
            ),
        )
        embed.set_footer(text="via Open-Meteo (openpublicapis.com)")
        await ctx.send(embed=embed)

    # ── define (Free Dictionary) ───────────────────────────────
    @commands.hybrid_command(name="define", description="Definition of a word.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def define(self, ctx, word: str):
        """Dictionary lookup via the Free Dictionary API."""
        async with ctx.typing():
            data = await self._session_get(
                f"https://api.dictionaryapi.dev/api/v2/entries/en/{word.strip().lower()}"
            )
        if not data:
            await ctx.send(f"📖 No definitions found for **{word}**.")
            return
        entry = data[0]
        phonetic = entry.get("phonetic") or next(
            (p.get("text") for p in entry.get("phonetics", []) if p.get("text")), "")
        embed = discord.Embed(
            title=f"📖 {entry.get('word', word)}",
            description=f"*/{phonetic}/*" if phonetic else "",
            color=discord.Color.green(),
        )
        for meaning in entry.get("meanings", [])[:2]:
            part = meaning.get("partOfSpeech", "")
            for definition in meaning.get("definitions", [])[:3]:
                text = definition.get("definition")
                example = definition.get("example")
                line = f"• {text}"
                if example:
                    line += f"\n  *“{example}”*"
                embed.add_field(name=f"_{part}_", value=line, inline=False)
        embed.set_footer(text="via dictionaryapi.dev (openpublicapis.com)")
        await ctx.send(embed=embed)

    # ── meme ───────────────────────────────────────────────────
    @commands.hybrid_command(name="meme", description="A random meme from Reddit.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def meme(self, ctx):
        """Random meme via meme-api.com."""
        async with ctx.typing():
            data = await self._session_get("https://meme-api.com/gimme")
        if not data or not data.get("url"):
            await ctx.send("⚠️ Couldn't fetch a meme right now.")
            return
        embed = discord.Embed(
            title=data.get("title", "Meme"),
            url=data.get("postLink", ""),
            color=discord.Color.purple(),
        )
        embed.set_image(url=data["url"])
        embed.set_footer(text=f"r/{data.get('subreddit', '?')} · 👍 {data.get('ups', '?')}")
        await ctx.send(embed=embed)

    # ── crypto (CoinGecko) ─────────────────────────────────────
    @commands.hybrid_command(name="crypto", description="Live price of a cryptocurrency.")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def crypto(self, ctx, coin: str = "bitcoin"):
        """CoinGecko spot price — /crypto btc, /crypto sol, ..."""
        coin_id = COIN_ALIASES.get(coin.strip().lower(), coin.strip().lower())
        async with ctx.typing():
            data = await self._session_get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd",
                        "include_24hr_change": "true"},
            )
        if not data or coin_id not in data:
            await ctx.send(f"🪙 No coin **{coin}**. Try /crypto btc, eth, sol, xrp, ada, doge...")
            return
        row = data[coin_id]
        price = row.get("usd")
        change = row.get("usd_24h_change")
        change_line = ""
        if change is not None:
            arrow = "📈" if change >= 0 else "📉"
            change_line = f"\n{arrow} {change:+.2f}% (24h)"
        embed = discord.Embed(
            title=f"🪙 {coin_id.replace('-', ' ').title()}",
            description=f"**${price:,.2f}**{change_line}",
            color=discord.Color.gold(),
        )
        embed.set_footer(text="via CoinGecko (openpublicapis.com)")
        await ctx.send(embed=embed)

    # ── spacex ─────────────────────────────────────────────────
    @commands.hybrid_command(name="spacex", description="Latest SpaceX launch.")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def spacex(self, ctx):
        """Latest launch info via the official SpaceX API."""
        async with ctx.typing():
            data = await self._session_get("https://api.spacexdata.com/v5/launches/latest")
        if not data:
            await ctx.send("⚠️ SpaceX API didn't answer. Try again later.")
            return
        name = data.get("name", "Unknown launch")
        flight = data.get("flight_number")
        date = str(data.get("date_utc", ""))[:10]
        details = data.get("details") or "No details provided."
        links = data.get("links") or {}
        patch = (links.get("patch") or {}).get("small")
        embed = discord.Embed(
            title=f"🚀 {name}",
            description=details[:900],
            color=discord.Color.dark_gray(),
        )
        embed.add_field(name="Flight", value=flight, inline=True)
        embed.add_field(name="Date (UTC)", value=date, inline=True)
        if links.get("webcast"):
            embed.add_field(name="Webcast", value=f"[Watch]({links['webcast']})", inline=True)
        if patch:
            embed.set_thumbnail(url=patch)
        embed.set_footer(text="via api.spacexdata.com (openpublicapis.com)")
        await ctx.send(embed=embed)

    # ── github ─────────────────────────────────────────────────
    @commands.hybrid_command(name="github", description="Public GitHub profile for a user.")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def github(self, ctx, user: str):
        """GitHub public profile via api.github.com (unauthenticated)."""
        async with ctx.typing():
            data = await self._session_get(f"https://api.github.com/users/{user.strip().lstrip('@')}")
        if not data:
            await ctx.send(f"🐙 No GitHub user **{user}** found (or rate-limited).")
            return
        name = data.get("name") or data.get("login")
        embed = discord.Embed(
            title=f"🐙 {name}",
            url=data.get("html_url", ""),
            description=data.get("bio") or data.get("login"),
            color=discord.Color(0x24292F),
        )
        embed.set_thumbnail(url=data.get("avatar_url", ""))
        embed.add_field(name="Followers", value=data.get("followers", 0), inline=True)
        embed.add_field(name="Following", value=data.get("following", 0), inline=True)
        embed.add_field(name="Public repos", value=data.get("public_repos", 0), inline=True)
        if data.get("location"):
            embed.add_field(name="📍 Location", value=data["location"], inline=False)
        embed.set_footer(text="via api.github.com (openpublicapis.com)")
        await ctx.send(embed=embed)

    # ── advice ─────────────────────────────────────────────────
    @commands.hybrid_command(name="advice", description="A random piece of advice.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def advice(self, ctx):
        """Random advice via the Advice Slip API."""
        data = await self._session_get("https://api.adviceslip.com/advice")
        if not data:
            await ctx.send("⚠️ No advice available right now.")
            return
        slip = (data or {}).get("slip") or {}
        await ctx.send(f"💡 **{slip.get('advice', '...')}**")

    # ── lyrics (LRCLIB) ────────────────────────────────────────
    @commands.hybrid_command(name="lyrics",
                             description="Lyrics for a song — omit the name to use the current track.")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def lyrics(self, ctx, *, query: str | None = None):
        """Search LRCLIB for lyrics. With no query, look up the current track."""
        if not query:
            music = self.bot.get_cog("Music")
            player = music and music.players.get(ctx.guild.id)
            if player is None or player.current is None:
                await ctx.send("🎤 Give a song name, or play something first:\n"
                               "`/lyrics imagine dragons believer`")
                return
            track = player.current
            query = f"{track.artist} {track.title}".strip() or track.title
            label = track.title
        else:
            label = query
        async with ctx.typing():
            data = await self._session_get(
                "https://lrclib.net/api/search", params={"q": query})
        if not data:
            await ctx.send(f"🎤 No lyrics found for **{label}**.")
            return
        track = data[0]  # best match first
        text = track.get("plainLyrics")
        if not text:
            synced = track.get("syncedLyrics")
            text = synced or "Lyrics found but not available as text."
        title = track.get("trackName", label)
        artist = track.get("artistName", "?")
        album = track.get("albumName") or "Single"
        embed = discord.Embed(
            title=f"🎤 {artist} — {title}",
            description=f"*{album}*\n\n{text[:3800]}",
            color=discord.Color.pink(),
        )
        embed.set_footer(text="via LRCLIB (openpublicapis.com)")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Apis(bot))