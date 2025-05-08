#!/usr/bin/env python3
import os, json, base64, logging, asyncio
from datetime import datetime
from typing import List, Dict

import openai, requests
from bs4 import BeautifulSoup
from pytz import timezone
from telegram import Bot
from telegram.error import TelegramError
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIGURATION ──────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger()

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SERVICE_ACCOUNT_JSON_B64 = os.getenv("SERVICE_ACCOUNT_JSON_B64", "")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_SHEET_CONFIG_TAB = os.getenv("GOOGLE_SHEET_CONFIG_TAB", "Config")
GOOGLE_SHEET_APP_TAB = os.getenv("GOOGLE_SHEET_APP_TAB", "Applications")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# Initialize clients
bot = Bot(TELEGRAM_TOKEN)
openai.api_key = OPENAI_API_KEY

# Decode and build Google services
svc_info = json.loads(base64.b64decode(SERVICE_ACCOUNT_JSON_B64))
creds = service_account.Credentials.from_service_account_info(
    svc_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
)
sheets = build("sheets", "v4", credentials=creds)
drive = build("drive", "v3", credentials=creds)

# HTTP headers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    )
}

async def send_telegram(message: str):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info("Telegram sent: " + message)
    except TelegramError as e:
        logger.error("Telegram error: " + str(e))

def read_config() -> Dict[str, str]:
    cfg = {}
    try:
        rows = sheets.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"{GOOGLE_SHEET_CONFIG_TAB}!A:B"
        ).execute().get("values", [])
        for key, val in rows:
            cfg[key] = val
    except Exception as e:
        logger.error("Failed reading config: " + str(e))
    return cfg

def fetch_jobs(sources: List[Dict[str, str]]) -> List[Dict]:
    jobs = []
    for src in sources:
        try:
            logger.info(f"Scraping {src['name']}")
            r = requests.get(src["url"], headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            before = len(jobs)
            if src["name"] == "LinkedIn":
                for li in soup.select("ul.jobs-search__results-list li"):
                    a = li.select_one("a.base-card__full-link")
                    t = li.select_one("h3.base-search-card__title")
                    if a and t:
                        jobs.append({"source":"LinkedIn","title":t.text.strip(),"link":a['href']})
            # Add other sources similarly...
            count = len(jobs) - before
            logger.info(f"{src['name']}: found {count}")
        except Exception as e:
            logger.error(f"{src['name']} error: {e}")
    return jobs

def load_seen() -> set:
    seen = set()
    try:
        rows = sheets.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"{GOOGLE_SHEET_APP_TAB}!C:C"
        ).execute().get("values", [])
        for [link] in rows:
            seen.add(link)
    except:
        pass
    return seen

async def main():
    await send_telegram("🔔 Pipeline started")
    cfg = read_config()
    sources = json.loads(cfg.get("sources_json","[]"))
    jobs = fetch_jobs(sources)
    seen = load_seen()
    new_jobs = [j for j in jobs if j["link"] not in seen]
    if not new_jobs:
        await send_telegram("🔔 No new jobs found.")
        return
    # Insert new rows at top
    meta = sheets.spreadsheets().get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
    sheet_id = next(s["properties"]["sheetId"] for s in meta["sheets"]
                    if s["properties"]["title"] == GOOGLE_SHEET_APP_TAB)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=GOOGLE_SHEETS_ID,
        body={"requests":[{"insertDimension":{
            "range":{"sheetId":sheet_id,"dimension":"ROWS","startIndex":1,"endIndex":1+len(new_jobs)},
            "inheritFromBefore":False
        }}]}
    ).execute()
    # Write values
    now = datetime.now(timezone("Asia/Jerusalem")).isoformat()
    values = [[j["source"],j["title"],j["link"],now,"NEW"] for j in new_jobs]
    sheets.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range=f"{GOOGLE_SHEET_APP_TAB}!A2:E{1+len(values)}",
        valueInputOption="RAW",
        body={"values":values}
    ).execute()
    # Notify
    msg = ["🔔 New jobs:"]
    msg += [f"{i+1}. {j['title']}" for i,j in enumerate(new_jobs)]
    await send_telegram("\n".join(msg))
    # Cache
    with open("new_jobs_cache.json","w",encoding="utf8") as f:
        json.dump(new_jobs, f, ensure_ascii=False)

if __name__ == "__main__":
    import asyncio
    from pytz import timezone
    asyncio.run(main())
