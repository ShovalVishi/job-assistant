import os
import asyncio
from telegram import Bot

# Load your bot token from the environment (or paste it directly)
TOKEN = os.getenv("TELEGRAM_TOKEN") or "<8170119843:AAGkW17WJxWGVzrXdyfJsuJgIjapiyju2Zg>"

bot = Bot(token=TOKEN)

async def main():
    updates = await bot.get_updates()
    if not updates:
        print("No updates yet—send any message to your bot first (e.g. “hi”).")
    else:
        # The chat ID is in the most recent message
        chat_id = updates[-1].message.chat.id
        print("Your chat ID is:", chat_id)

if __name__ == "__main__":
    asyncio.run(main())
