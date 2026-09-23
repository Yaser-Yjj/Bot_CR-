import random

import discord
from discord.ext import commands

import config

EIGHTBALL_ANSWERS = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes — definitely.",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Don't count on it.",
    "My reply is no.", "My sources say no.", "Outlook not so good.", "Very doubtful.",
]

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs. 🐛",
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my robot a joke... it went over its head. 🤖",
    "Why was the robot angry? Because someone kept pushing its buttons.",
    "There are only 10 types of people: those who understand binary, and those who don't.",
    "Why did the developer go broke? Because he used up all his cache. 💸",
    "A SQL query walks into a bar, goes up to two tables and asks: 'Can I JOIN you?'",
    "Why do we tell actors to 'break a leg'? Because every play has a cast. 🎭",
]

FACTS = [
    "Honey never spoils — archaeologists have found 3,000-year-old honey in Egyptian tombs.",
    "Octopuses have three hearts and blue blood.",
    "A day on Venus is longer than a year on Venus.",
    "The Eiffel Tower grows about 15 cm taller in summer due to thermal expansion.",
    "Bananas are berries, but strawberries are not.",
    "Your brain uses about 20% of your body's total energy.",
    "There are more possible chess games than atoms in the observable universe. ♟️",
    "Lightning strikes the Earth about 100 times every second. ⚡",
]

COMPLIMENTS = [
    "You have an amazing sense of humour!", "Your ideas are genuinely brilliant.",
    "You make this server a better place. 💛", "Your code is cleaner than a sorted array.",
    "You light up every conversation you join.", "You're the kind of person others look up to.",
    "Your vibes are immaculate. ✨",
]

SLAPS = [
    "{actor} slapped {target} with a servo motor! ⚙️",
    "{actor} slapped {target} with a fish. 🐟",
    "{actor} slapped {target} with a rolled-up user manual.",
    "{actor} slapped {target} so hard their Discord lagged.",
    "{actor} gently slapped {target} with a keyboard. ⌨️",
]

HUGS = [
    "{actor} gave {target} a warm hug. 🤗",
    "{actor} hugged {target} like they just won a hackathon. 🏆",
    "{actor} wrapped {target} in a group-hug energy. 💞",
    "{actor} gave {target} a robot-arm hug. 🦾",
]

ROASTS = [
    "You're like a 404 error — not found in my list of concerns.",
    "You bring everyone joy... when you leave the room. 😄",
    "Your brain is the size of a microcontroller with no program loaded.",
    "You're the reason they put instructions on shampoo bottles.",
    "I'd roast you, but my circuits can't handle that much cringe.",
    "You're like a robot without batteries — full of potential, zero output.",
    "You're the human equivalent of a segfault. 💥",
]

QUOTES = [
    "The best way to predict the future is to invent it. — Alan Kay",
    "First, solve the problem. Then, write the code. — John Johnson",
    "Innovation distinguishes between a leader and a follower. — Steve Jobs",
    "Programs must be written for people to read. — Harold Abelson",
    "Simplicity is the soul of efficiency. — Austin Freeman",
    "The most disastrous thing that you can ever learn is your first programming language. — Alan Kay",
    "Feedback is a gift. Even when it's wrapped in an exception. 🎁",
]


class Fun(commands.Cog):
    """Lighthearted commands to keep the server lively."""

    def __init__(self, bot):
        self.bot = bot
        self.quiz_runs: dict = {}

    @commands.hybrid_command(name="8ball", description="Ask the magic 8-ball a question.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def eight_ball(self, ctx, *, question: str):
        await ctx.send(f"❓ {ctx.author.display_name} asked: *{question}*\n🎱 {random.choice(EIGHTBALL_ANSWERS)}")

    @commands.hybrid_command(name="coinflip", description="Flip a coin.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def coinflip(self, ctx):
        await ctx.send(f"🪙 The coin lands on: **{random.choice(['Heads', 'Tails'])}**!")

    @commands.hybrid_command(name="dice", description="Roll a die (default 6 sides).")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def dice(self, ctx, sides: int = 6):
        sides = max(2, min(sides, 1000))
        await ctx.send(f"🎲 You rolled a **{random.randint(1, sides)}** (d{sides})!")

    @commands.hybrid_command(name="slap", description="Slap someone.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def slap(self, ctx, member: discord.Member):
        if member == self.bot.user:
            await ctx.send("Nice try, but I don't feel a thing. 🤖💢")
            return
        template = random.choice(SLAPS)
        await ctx.send(template.format(actor=ctx.author.display_name, target=member.display_name))

    @commands.hybrid_command(name="hug", description="Hug someone.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def hug(self, ctx, member: discord.Member):
        template = random.choice(HUGS)
        await ctx.send(template.format(actor=ctx.author.display_name, target=member.display_name))

    @commands.hybrid_command(name="joke", description="Tell a random tech joke.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def joke(self, ctx):
        await ctx.send(random.choice(JOKES))

    @commands.hybrid_command(name="fact", description="Share a random fun fact.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def fact(self, ctx):
        await ctx.send(f"💡 Fun fact: {random.choice(FACTS)}")

    @commands.hybrid_command(name="compliment", description="Get a compliment.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def compliment(self, ctx):
        await ctx.send(f"💛 {ctx.author.mention}, {random.choice(COMPLIMENTS)}")

    # ── club website ───────────────────────────────────────────
    @commands.hybrid_command(name="website", description="The Robotics Club's website.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def website(self, ctx):
        embed = discord.Embed(
            title="🌐 Robotics Club",
            description="Projects, teams, events and how to join — all on our site.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="🔗 Website", value=config.WEBSITE_URL, inline=False)
        embed.set_footer(text="Come check out what we're building!")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Visit robotics.ma", url=config.WEBSITE_URL,
            style=discord.ButtonStyle.link,
        ))
        await ctx.send(embed=embed, view=view)

    # ── moar fun ───────────────────────────────────────────────
    @commands.hybrid_command(name="rps", description="Play rock-paper-scissors against the bot.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def rps(self, ctx, choice: str):
        choice = choice.strip().lower()
        emoji = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        if choice not in emoji:
            await ctx.send("⚠️ Pick `rock`, `paper` or `scissors`.")
            return
        bot_choice = random.choice(list(emoji))
        beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
        outcome = "It's a tie! 🤝"
        if beats[choice] == bot_choice:
            outcome = "You win! 🎉"
        elif beats[bot_choice] == choice:
            outcome = "I win! 🤖"
        await ctx.send(f"{emoji[choice]} vs {emoji[bot_choice]} — {outcome}")

    @commands.hybrid_command(name="ship", description="Ship two members with a compatibility score.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ship(self, ctx, first: discord.Member, second: discord.Member):
        score = random.randint(1, 100)
        heart = "❤️" * max(1, score // 20)
        mood = "💔" if score < 40 else ("💕" if score < 75 else "💘")
        await ctx.send(
            f"{mood} **{first.display_name}** x **{second.display_name}** — "
            f"{score}% compatible {heart}"
        )

    @commands.hybrid_command(name="choose", description="The bot picks one of your options.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def choose(self, ctx, options: str):
        choices = [option for option in options.replace(",", " ").split() if option.strip()]
        if not choices:
            await ctx.send("⚠️ Give me some options: `!choose pizza sushi tacos`")
            return
        await ctx.send(f"🧠 I choose: **{random.choice(choices)}**")

    @commands.hybrid_command(name="reverse", description="Reverse your text.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def reverse(self, ctx, *, text: str):
        await ctx.send(f"↩️ {text[::-1]}")

    @commands.hybrid_command(name="clap", description="👏 Emphasis 👏 on 👏 every 👏 word.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def clap(self, ctx, *, text: str):
        words = text.split()
        if not words:
            await ctx.send("⚠️ Give me a phrase to clapify.")
            return
        await ctx.send("👏 " + " 👏 ".join(words) + " 👏")

    @commands.hybrid_command(name="roast", description="Roast someone (playfully).")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def roast(self, ctx, member: discord.Member):
        if member == self.bot.user:
            await ctx.send("I'm immune to roasts. 🤖🔥")
            return
        await ctx.send(f"{member.mention}, {random.choice(ROASTS)}")

    @commands.hybrid_command(name="quote", description="A random tech/robotics quote.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def quote(self, ctx):
        await ctx.send(f"💬 {random.choice(QUOTES)}")

    # ── robot status ──────────────────────────────────────────
    @commands.hybrid_command(name="robot", description="Check on the club's robot buddy.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def robot(self, ctx):
        """A playful telemetry readout for the club mascot."""
        battery = random.randint(40, 100)
        sensors = random.randint(3, 4)
        motor = "CALIBRATING" if random.random() < 0.12 else "ONLINE"
        lines = [
            "🤖 **ROBOT STATUS**",
            f"CPU:        ONLINE",
            f"Motors:     {motor}",
            f"Sensors:    {sensors}/4",
            f"Battery:    {battery}%",
            "",
            "System nominal." if motor == "ONLINE" else "One motor recalibrating — all good. 🔧",
            f"*Ping: {round(self.bot.latency * 1000)} ms*" if hasattr(self.bot, "latency") else "",
        ]
        await ctx.send("```\n" + "\n".join(lines) + "\n```")

    # ── robotics quiz ─────────────────────────────────────────
    @commands.hybrid_command(name="quiz", description="A 5-question robotics + engineering quiz.")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def quiz(self, ctx):
        """Five multiple-choice questions; the view tracks your score."""
        questions = random.sample(QUIZ_BANK, min(5, len(QUIZ_BANK)))
        if self.quiz_runs is None:
            self.quiz_runs = {}
        self.quiz_runs[ctx.author.id] = {"questions": questions, "index": 0, "score": 0}
        first = questions[0]
        embed = quiz_embed(first, 1, len(questions), 0)
        await ctx.send(embed=embed, view=QuizView(self, ctx.author.id))


class QuizView(discord.ui.View):
    """Four-option multiple choice; advances through the quiz and scores it."""

    LETTERS = ("🇦", "🇧", "🇨", "🇩")

    def __init__(self, cog: Fun, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

    def _state(self):
        return self.cog.quiz_runs.get(self.user_id) or {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "🔒 This quiz belongs to someone else — run `/quiz` yourself!",
                ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🇦", style=discord.ButtonStyle.secondary, custom_id="quiz:0")
    async def a(self, interaction, button):
        await self._answer(interaction, 0)

    @discord.ui.button(label="🇧", style=discord.ButtonStyle.secondary, custom_id="quiz:1")
    async def b(self, interaction, button):
        await self._answer(interaction, 1)

    @discord.ui.button(label="🇨", style=discord.ButtonStyle.secondary, custom_id="quiz:2")
    async def c(self, interaction, button):
        await self._answer(interaction, 2)

    @discord.ui.button(label="🇩", style=discord.ButtonStyle.secondary, custom_id="quiz:3")
    async def d(self, interaction, button):
        await self._answer(interaction, 3)

    async def _answer(self, interaction, choice: int):
        state = self._state()
        questions = state.get("questions") or []
        index = state.get("index", 0)
        if index >= len(questions):
            await interaction.response.edit_message(embed=self._summary(state), view=None)
            return
        question = questions[index]
        correct = choice == question["answer"]
        if correct:
            state["score"] = state.get("score", 0) + 1
        state["index"] = index + 1
        await interaction.response.edit_message(
            embed=quiz_result_embed(question, choice, state["score"],
                                    state["index"], len(questions)),
            view=self)
        if state["index"] >= len(questions):
            self.cog.quiz_runs.pop(self.user_id, None)
            await interaction.followup.send(
                f"📊 Final score: **{state['score']}/{len(questions)}**", ephemeral=True)

    def _summary(self, state):
        return discord.Embed(
            title="🏁 Quiz complete!",
            description=f"Score: **{state.get('score', 0)}/{len(state.get('questions') or [])}** 🎉",
            color=discord.Color.green())


def quiz_embed(question: dict, number: int, total: int, score: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔬 Robotics Quiz — Q{number}/{total}",
        description=question["q"],
        color=discord.Color.blue(),
    )
    for i, option in enumerate(question["options"]):
        embed.add_field(name=f"Option {QuizView.LETTERS[i]}", value=option, inline=True)
    if score:
        embed.set_footer(text=f"Score: {score}")
    return embed


def quiz_result_embed(question: dict, choice: int, score: int, number: int,
                     total: int) -> discord.Embed:
    correct = choice == question["answer"]
    embed = discord.Embed(
        title=f"{'✅ Correct!' if correct else '❌ Not quite.'}",
        description=f"**{question['q']}**\n\n"
                    f"{QuizView.LETTERS[choice]} {question['options'][choice]} "
                    f"{'✔️' if correct else '✘'}\n"
                    f"*{question.get('explain', '')}*",
        color=discord.Color.green() if correct else discord.Color.red(),
    )
    if number < total:
        embed.set_footer(text=f"Score: {score} · next question coming up…")
    return embed


QUIZ_BANK = [
    {
        "q": "A PID controller has three terms. What does 'D' stand for?",
        "options": ["Damping", "Derivative", "Delay", "Distance"],
        "answer": 1,
        "explain": "Derivative — it anticipates the error's rate of change to dampen overshoot.",
    },
    {
        "q": "Which sensor would you use to measure how far the robot is from a wall?",
        "options": ["Gyroscope", "Ultrasonic sensor", "Thermistor", "Encoder"],
        "answer": 1,
        "explain": "Ultrasonic sensors measure distance via sound-wave time-of-flight.",
    },
    {
        "q": "In a DC motor, what does an H-bridge do?",
        "options": ["Changes the motor's speed", "Reverses the motor's direction",
                    "Cools the motor", "Amplifies voltage"],
        "answer": 1,
        "explain": "An H-bridge switches the polarity of the supply, reversing rotation.",
    },
    {
        "q": "What unit is rotational speed measured in?",
        "options": ["RPM", "Volts", "Ohms", "Hertz"],
        "answer": 0,
        "explain": "RPM = revolutions per minute.",
    },
    {
        "q": "Which protocol is commonly used to daisy-chain many servos?",
        "options": ["I2C", "PWM directly", "UART only", "SPI only"],
        "answer": 0,
        "explain": "I2C uses addressable buses so many devices share two wires.",
    },
    {
        "q": "What does 'GPIO' stand for?",
        "options": ["General Purpose Input/Output", "Great Power In One",
                    "General Processing Input Only", "Giga-Power I/O"],
        "answer": 0,
        "explain": "GPIO pins can be configured as inputs or outputs.",
    },
    {
        "q": "A gear ratio of 3:1 (driver:driven) gives the output what?",
        "options": ["3× speed, ⅓ torque", "⅓ speed, 3× torque",
                    "Same speed, 3× torque", "3× speed and torque"],
        "answer": 1,
        "explain": "Reduction gearing trades speed for torque.",
    },
    {
        "q": "Which of these is a brushless motor controller?",
        "options": ["ESC", "LDR", "NPN transistor", "Capacitor"],
        "answer": 0,
        "explain": "ESC — Electronic Speed Controller — drives brushless motors.",
    },
    {
        "q": "What signal does an encoder output?",
        "options": ["Pulses proportional to rotation", "Analog voltage only",
                    "Radio waves", "A PID value"],
        "answer": 0,
        "explain": "Encoders emit pulses that count rotation or position.",
    },
    {
        "q": "Voltage × Current = ?",
        "options": ["Resistance", "Power", "Charge", "Torque"],
        "answer": 1,
        "explain": "P = V × I, measured in watts.",
    },
]


async def setup(bot):
    await bot.add_cog(Fun(bot))