#!/usr/bin/env python3
import os, json, logging
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from job_assistant import generate_documents
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# Env
TELE_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELE_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
SHEET_ID   = os.getenv("GOOGLE_SHEETS_ID")
APP_TAB    = os.getenv("GOOGLE_SHEET_APP_TAB","Applications")
B64        = os.getenv("SERVICE_ACCOUNT_JSON_B64","")

# Setup
bot = Bot(TELE_TOKEN)
info = json.loads(base64.b64decode(B64))
creds = service_account.Credentials.from_service_account_info(
    info,
    scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
)
sheets = build("sheets","v4",creds)

async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        idxs = [int(x)-1 for x in text.split()]
        jobs = json.load(open("new_jobs_cache.json","r",encoding="utf8"))
        chosen = [jobs[i] for i in idxs]
    except:
        return await update.message.reply_text("Send job numbers e.g. '1 3'")

    values = []
    for job in chosen:
        docs = generate_documents(job)
        now = datetime.now().isoformat()
        values.append([job["source"],job["title"],job["link"],now,"SUBMITTED",docs["resume"],docs["cover"]])

    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{APP_TAB}!A:G",
        valueInputOption="RAW",
        body={"values":values}
    ).execute()

    await update.message.reply_text(f"✅ Submitted {len(values)}.")

app = ApplicationBuilder().token(TELE_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
app.run_polling()
