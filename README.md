# Crew Me

A Discord staff management bot with activity tracking, absences,
meetings, warnings, staff tickets, and management review workflows.

## Features

- Staff activity tracking and weekly evaluations
- Configurable staff roles and time zones
- Absence management
- Staff warnings and meetings
- Demotion review embeds with Reviewed and Dismiss buttons
- Persistent review records and notification tracking

## Requirements

- Python 3.10 or newer
- discord.py
- A Discord bot application and token

## Configuration

Create a `.env` file containing your Discord bot token:

    DISCORD_TOKEN=your_bot_token_here

Never commit `.env`, database files, or other secrets.

## Run

Install dependencies and start the bot:

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    python bot.py
