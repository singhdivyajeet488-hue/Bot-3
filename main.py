# main.py
import discord
from discord import app_commands
from discord.ui import Button, View
import asyncio
import os
import re
import time
import json
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
import aiohttp
import subprocess
from gtts import gTTS
import io
import ffmpeg
import threading
import queue
import collections

# ============================================
# CONFIGURATION
# ============================================
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable not set")

# ============================================
# GLOBAL STATE
# ============================================
class BotState:
    def __init__(self):
        self.polls: Dict[int, 'Poll'] = {}  # channel_id -> Poll
        self.tts_sessions: Dict[int, 'TTSSession'] = {}  # guild_id -> TTSSession
        self.mc_status_tasks: Dict[int, asyncio.Task] = {}  # message_id -> task
        self.mc_status_data: Dict[int, Dict] = {}  # message_id -> data

state = BotState()

# ============================================
# POLL SYSTEM
# ============================================
class Poll:
    def __init__(self, question: str, options: List[str], duration: int, creator_id: int, channel_id: int):
        self.question = question
        self.options = options
        self.duration = duration
        self.creator_id = creator_id
        self.channel_id = channel_id
        self.votes: Dict[int, int] = {}  # user_id -> option_index
        self.start_time = datetime.now()
        self.end_time = self.start_time + timedelta(minutes=duration)
        self.ended = False
        self.message_id: Optional[int] = None

    def get_results(self) -> Dict[str, any]:
        total_votes = len(self.votes)
        option_counts = [0] * len(self.options)
        for user_id, choice in self.votes.items():
            if 0 <= choice < len(self.options):
                option_counts[choice] += 1
        
        percentages = []
        for count in option_counts:
            if total_votes > 0:
                percentages.append((count / total_votes) * 100)
            else:
                percentages.append(0)
        
        return {
            'total_votes': total_votes,
            'option_counts': option_counts,
            'percentages': percentages
        }

    def get_winners(self) -> List[int]:
        counts = self.get_results()['option_counts']
        if not counts:
            return []
        max_votes = max(counts)
        if max_votes == 0:
            return []
        return [i for i, count in enumerate(counts) if count == max_votes]

class PollView(View):
    def __init__(self, poll: Poll, bot):
        super().__init__(timeout=None)
        self.poll = poll
        self.bot = bot
        
        for i, option in enumerate(poll.options):
            button = Button(
                label=option,
                style=discord.ButtonStyle.primary,
                custom_id=f"poll_{i}"
            )
            button.callback = self.create_callback(i)
            self.add_item(button)
    
    def create_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if self.poll.ended:
                await interaction.response.send_message("This poll has ended.", ephemeral=True)
                return
            
            user_id = interaction.user.id
            
            # Remove previous vote if exists
            if user_id in self.poll.votes:
                if self.poll.votes[user_id] == index:
                    await interaction.response.send_message("You already voted for this option.", ephemeral=True)
                    return
            
            self.poll.votes[user_id] = index
            await self.update_poll_message(interaction)
            await interaction.response.send_message(f"Voted for: {self.poll.options[index]}", ephemeral=True)
        
        return callback
    
    async def update_poll_message(self, interaction: discord.Interaction):
        embed, view = create_poll_embed(self.poll)
        channel = interaction.channel
        try:
            message = await channel.fetch_message(self.poll.message_id)
            await message.edit(embed=embed, view=view)
        except:
            pass

def create_poll_embed(poll: Poll) -> tuple:
    results = poll.get_results()
    embed = discord.Embed(
        title=f"📊 {poll.question}",
        color=discord.Color.blue()
    )
    
    max_votes = max(results['option_counts']) if results['option_counts'] else 0
    bar_length = 20
    
    for i, option in enumerate(poll.options):
        count = results['option_counts'][i]
        percentage = results['percentages'][i]
        
        # Create progress bar
        filled = int((percentage / 100) * bar_length) if percentage > 0 else 0
        bar = "█" * filled + "░" * (bar_length - filled)
        
        embed.add_field(
            name=option,
            value=f"`{bar}` {count} votes ({percentage:.1f}%)",
            inline=False
        )
    
    embed.add_field(
        name="Total unique votes",
        value=str(results['total_votes']),
        inline=False
    )
    
    if poll.ended:
        winners = poll.get_winners()
        if winners:
            winner_names = [poll.options[i] for i in winners]
            embed.add_field(
                name="🏆 Winner(s)",
                value=", ".join(winner_names),
                inline=False
            )
        embed.set_footer(text="Poll ended.")
    else:
        time_left = (poll.end_time - datetime.now()).total_seconds()
        minutes = int(time_left // 60)
        seconds = int(time_left % 60)
        embed.set_footer(text=f"Time remaining: {minutes}m {seconds}s")
    
    view = PollView(poll, None) if not poll.ended else None
    return embed, view

# ============================================
# TTS VOICE CHAT SYSTEM
# ============================================
class TTSSession:
    def __init__(self, guild_id: int, text_channel_id: int, voice_client: discord.VoiceClient):
        self.guild_id = guild_id
        self.text_channel_id = text_channel_id
        self.voice_client = voice_client
        self.queue: asyncio.Queue = asyncio.Queue()
        self.current_task: Optional[asyncio.Task] = None
        self.is_playing = False
        self.should_stop = False
        self.lock = asyncio.Lock()
    
    async def add_to_queue(self, text: str):
        await self.queue.put(text)
        if not self.is_playing:
            await self.process_queue()
    
    async def process_queue(self):
        async with self.lock:
            if self.is_playing:
                return
            self.is_playing = True
        
        try:
            while not self.queue.empty() and not self.should_stop:
                text = await self.queue.get()
                await self.play_tts(text)
                self.queue.task_done()
        finally:
            async with self.lock:
                self.is_playing = False
                if self.should_stop:
                    self.should_stop = False
    
    async def play_tts(self, text: str):
        try:
            # Detect language
            if re.search(r'[\u0900-\u097F]', text):
                lang = 'hi'
            else:
                lang = 'en'
            
            # Generate TTS
            tts = gTTS(text=text, lang=lang, slow=False)
            audio_data = io.BytesIO()
            tts.write_to_fp(audio_data)
            audio_data.seek(0)
            
            # Convert to proper format
            audio_source = discord.FFmpegPCMAudio(audio_data, pipe=True)
            
            # Play audio
            self.voice_client.play(audio_source, after=lambda e: None)
            while self.voice_client.is_playing() and not self.should_stop:
                await asyncio.sleep(0.1)
            
            if self.should_stop:
                self.voice_client.stop()
                
        except Exception as e:
            print(f"TTS error: {e}")
    
    async def stop(self):
        self.should_stop = True
        if self.voice_client.is_playing():
            self.voice_client.stop()
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except:
                break
        if self.current_task:
            self.current_task.cancel()
    
    async def disconnect(self):
        await self.stop()
        if self.voice_client.is_connected():
            await self.voice_client.disconnect()

# ============================================
# MINECRAFT SERVER STATUS
# ============================================
async def get_minecraft_status(server_ip: str) -> Dict:
    try:
        import mcstatus
        server = mcstatus.JavaServer(server_ip)
        status = await server.async_status()
        
        return {
            'online': True,
            'version': status.version.name,
            'players_online': status.players.online,
            'players_max': status.players.max,
            'motd': status.description,
            'latency': status.latency,
            'favicon': status.favicon if hasattr(status, 'favicon') else None
        }
    except Exception as e:
        return {
            'online': False,
            'error': str(e)
        }

async def update_mc_status(bot: discord.Client, message_id: int, channel_id: int, server_ip: str):
    try:
        channel = bot.get_channel(channel_id)
        if not channel:
            return
        
        while True:
            try:
                message = await channel.fetch_message(message_id)
                status = await get_minecraft_status(server_ip)
                
                embed = create_mc_embed(server_ip, status)
                await message.edit(embed=embed)
                
                await asyncio.sleep(30)
            except Exception as e:
                print(f"MC Status update error: {e}")
                await asyncio.sleep(30)
    except Exception as e:
        print(f"MC Status loop error: {e}")

def create_mc_embed(server_ip: str, status: Dict) -> discord.Embed:
    embed = discord.Embed(
        title="🎮 Minecraft Server Connection Panel",
        color=discord.Color.green() if status.get('online', False) else discord.Color.red()
    )
    
    embed.add_field(name="📌 Server IP", value=server_ip, inline=False)
    
    if status.get('online', False):
        embed.add_field(name="⚙️ Server Version", value=status.get('version', 'Unknown'), inline=False)
        embed.add_field(
            name="👥 Members",
            value=f"{status.get('players_online', 0)}/{status.get('players_max', 0)} playing",
            inline=False
        )
        embed.add_field(name="Server MOTD", value=status.get('motd', 'No MOTD'), inline=False)
        embed.add_field(name="📶 Latency", value=f"{status.get('latency', 0)}ms", inline=False)
        embed.add_field(name="Status", value="🟢 Online", inline=False)
        
        # Add favicon if available
        if status.get('favicon'):
            embed.set_thumbnail(url=status['favicon'])
    else:
        embed.add_field(name="Status", value="🔴 Offline", inline=False)
        if status.get('error'):
            embed.add_field(name="Error", value=status['error'], inline=False)
    
    embed.set_footer(text="🔄 Seamlessly updating background loop active (30s intervals)")
    return embed

# ============================================
# DISCORD BOT
# ============================================
class DiscordBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        
        # Register commands
        self.register_commands()
    
    def register_commands(self):
        # Poll commands
        @self.tree.command(name="poll", description="Create a new poll")
        @app_commands.describe(
            question="The poll question",
            options="Options separated by commas (2-5)",
            duration="Duration in minutes"
        )
        async def poll_create(interaction: discord.Interaction, question: str, options: str, duration: int):
            await self.handle_poll_create(interaction, question, options, duration)
        
        @self.tree.command(name="poll_end", description="End an active poll")
        async def poll_end(interaction: discord.Interaction):
            await self.handle_poll_end(interaction)
        
        # TTS commands
        @self.tree.command(name="join", description="Bot joins your voice channel and reads messages")
        async def join_tts(interaction: discord.Interaction):
            await self.handle_join_tts(interaction)
        
        @self.tree.command(name="leave", description="Bot leaves the voice channel")
        async def leave_tts(interaction: discord.Interaction):
            await self.handle_leave_tts(interaction)
        
        @self.tree.command(name="stop", description="Stop TTS playback and clear queue")
        async def stop_tts(interaction: discord.Interaction):
            await self.handle_stop_tts(interaction)
        
        # MC Status command
        @self.tree.command(name="mcstatus", description="Check Minecraft server status")
        @app_commands.describe(server_ip="Minecraft server IP address")
        async def mcstatus(interaction: discord.Interaction, server_ip: str):
            await self.handle_mcstatus(interaction, server_ip)
    
    # ========================================
    # POLL HANDLERS
    # ========================================
    async def handle_poll_create(self, interaction: discord.Interaction, question: str, options: str, duration: int):
        if duration < 1 or duration > 1440:
            await interaction.response.send_message("Duration must be between 1 and 1440 minutes.", ephemeral=True)
            return
        
        option_list = [opt.strip() for opt in options.split(',') if opt.strip()]
        if len(option_list) < 2 or len(option_list) > 5:
            await interaction.response.send_message("Please provide 2-5 options separated by commas.", ephemeral=True)
            return
        
        poll = Poll(question, option_list, duration, interaction.user.id, interaction.channel_id)
        embed, view = create_poll_embed(poll)
        
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        poll.message_id = message.id
        
        state.polls[interaction.channel_id] = poll
        
        # Schedule auto-end
        async def end_poll():
            await asyncio.sleep(duration * 60)
            if not poll.ended:
                await self.end_poll(poll, interaction.channel)
        
        asyncio.create_task(end_poll())
    
    async def handle_poll_end(self, interaction: discord.Interaction):
        poll = state.polls.get(interaction.channel_id)
        if not poll:
            await interaction.response.send_message("No active poll in this channel.", ephemeral=True)
            return
        
        if poll.creator_id != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only the poll creator or administrators can end this poll.", ephemeral=True)
            return
        
        await self.end_poll(poll, interaction.channel)
        await interaction.response.send_message("Poll ended successfully.", ephemeral=True)
    
    async def end_poll(self, poll: Poll, channel: discord.TextChannel):
        if poll.ended:
            return
        
        poll.ended = True
        embed, view = create_poll_embed(poll)
        
        try:
            message = await channel.fetch_message(poll.message_id)
            await message.edit(embed=embed, view=None)
        except:
            pass
        
        # Clean up
        state.polls.pop(channel.id, None)
    
    # ========================================
    # TTS HANDLERS
    # ========================================
    async def handle_join_tts(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel.", ephemeral=True)
            return
        
        voice_channel = interaction.user.voice.channel
        
        # Check if already in a session
        if interaction.guild_id in state.tts_sessions:
            await interaction.response.send_message("Bot is already in a TTS session in this server.", ephemeral=True)
            return
        
        try:
            voice_client = await voice_channel.connect()
            session = TTSSession(interaction.guild_id, interaction.channel_id, voice_client)
            state.tts_sessions[interaction.guild_id] = session
            
            await interaction.response.send_message(f"Joined {voice_channel.name}. I will read messages in this channel.", ephemeral=True)
            
            # Store the text channel for message listening
            self.tts_text_channels[interaction.guild_id] = interaction.channel_id
            
        except Exception as e:
            await interaction.response.send_message(f"Failed to join voice channel: {e}", ephemeral=True)
    
    async def handle_leave_tts(self, interaction: discord.Interaction):
        session = state.tts_sessions.get(interaction.guild_id)
        if not session:
            await interaction.response.send_message("Bot is not in a TTS session.", ephemeral=True)
            return
        
        await session.disconnect()
        state.tts_sessions.pop(interaction.guild_id, None)
        self.tts_text_channels.pop(interaction.guild_id, None)
        
        await interaction.response.send_message("Left voice channel and cleared TTS queue.", ephemeral=True)
    
    async def handle_stop_tts(self, interaction: discord.Interaction):
        session = state.tts_sessions.get(interaction.guild_id)
        if not session:
            await interaction.response.send_message("Bot is not in a TTS session.", ephemeral=True)
            return
        
        await session.stop()
        await interaction.response.send_message("Stopped TTS playback and cleared queue.", ephemeral=True)
    
    # ========================================
    # MC STATUS HANDLERS
    # ========================================
    async def handle_mcstatus(self, interaction: discord.Interaction, server_ip: str):
        await interaction.response.defer()
        
        status = await get_minecraft_status(server_ip)
        embed = create_mc_embed(server_ip, status)
        
        message = await interaction.followup.send(embed=embed)
        
        # Start background update task
        task = asyncio.create_task(update_mc_status(self, message.id, interaction.channel_id, server_ip))
        state.mc_status_tasks[message.id] = task
    
    # ========================================
    # EVENTS
    # ========================================
    async def setup_hook(self):
        await self.tree.sync()
        self.tts_text_channels = {}
    
    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print(f"Synced {len(self.tree.get_commands())} commands")
    
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        
        # Check TTS
        if message.guild and message.guild.id in state.tts_sessions:
            session = state.tts_sessions[message.guild.id]
            if session.text_channel_id == message.channel.id:
                await session.add_to_queue(message.content)
    
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Auto-reconnect logic
        if member.id == self.user.id:
            if before.channel and not after.channel:
                # Bot was disconnected
                session = state.tts_sessions.get(member.guild.id)
                if session:
                    await session.disconnect()
                    state.tts_sessions.pop(member.guild.id, None)
                    self.tts_text_channels.pop(member.guild.id, None)
            
            elif after.channel and not before.channel:
                # Bot reconnected - handled by join command
                pass

# ============================================
# MAIN
# ============================================
bot = DiscordBot()
bot.run(TOKEN)
