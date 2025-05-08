import os
import json
import base64
import logging
import asyncio
from datetime import datetime
from typing import List, Dict

import openai
import requests
from bs4 import BeautifulSoup
from pytz import timezone

from telegram import Bot
from telegram.error import TelegramError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import smtplib

# ------------------ CONFIGURATION ------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
telegram_bot = Bot(token=TELEGRAM_TOKEN)

# Google credentials
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64','')
service_account_info = {}
try:
    service_account_info = json.loads(base64.b64decode(b64))
except Exception as e:
    logger.error(f"Invalid SERVICE_ACCOUNT_JSON_B64: {e}")
creds = None
if service_account_info:
    try:
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive']
        )
    except Exception as e:
        logger.error(f"Credentials error: {e}")
sheets_service = build('sheets','v4',credentials=creds) if creds else None
drive_service = build('drive','v3',credentials=creds) if creds else None

SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_ID')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')

# Name of desired sheet tab; fallback to first if not present
TARGET_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME')

# Job sources unchanged...
JOB_SOURCES = [
    # same list...
    {'name':'LinkedIn','url':'https://www.linkedin.com/jobs/search?keywords=Product%20Manager&location=Central%20Israel'},
    {'name':'AllJobs','url':'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product%20Manager&region=Center'},
    {'name':'Drushim','url':'https://www.drushim.co.il/jobs/?q=Product%20Manager&loc=Center'},
    {'name':'Indeed','url':'https://il.indeed.com/jobs?q=Product+Manager&l=Central+Israel'},
    {'name':'Glassdoor','url':'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'},
]

async def send_telegram(msg:str):
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
        logger.info(f"Telegram: {msg}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fetch_jobs()->List[Dict]:
    jobs=[]; 
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']}...")
        try:
            r=requests.get(src['url'],timeout=20); r.raise_for_status()
            soup=BeautifulSoup(r.text,'html.parser')
            before=len(jobs)
            # scraping logic same as before...
            # [Omitted for brevity]
            # Append into jobs list
        except Exception as e:
            logger.error(f"Scrape {src['name']} failed: {e}")
    logger.info(f"Total fetched: {len(jobs)}")
    return jobs

def filter_relevant(jobs:List[Dict])->List[Dict]:
    relevant=[]
    for job in jobs:
        try:
            prompt=(f"Job title: {job['title']}\n"
                    f"Link: {job['link']}\n"
                    "Location: within 1h drive of Netanya\n"
                    "Salary ~25000 ILS\n"
                    "Reply 'yes' or 'no'.")
            resp=openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user","content":prompt}]
            )
            if 'yes' in resp.choices[0].message.content.lower():
                relevant.append(job)
        except Exception:
            relevant.append(job)
    logger.info(f"Relevant: {len(relevant)}")
    return relevant

def generate_documents(job:Dict)->Dict[str,str]:
    # entails document generation and Drive upload
    return {'resume':'','cover':''}

async def apply_and_log(jobs:List[Dict],relevant:List[Dict]):
    # Get available sheet names
    try:
        meta=sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        sheets=[s['properties']['title'] for s in meta['sheets']]
    except Exception as e:
        logger.error(f"Failed to fetch sheets metadata: {e}")
        sheets=[]
    # Determine sheet tab
    if TARGET_SHEET_NAME in sheets:
        tab=TARGET_SHEET_NAME
    elif sheets:
        tab=sheets[0]
        logger.warning(f"{TARGET_SHEET_NAME or 'Default'} not found, using first tab '{tab}'")
    else:
        tab='Sheet1'
        logger.error("No sheets found, defaulting to 'Sheet1'")
    range_name=f"{tab}!A:E"
    rows=[]
    for job in relevant:
        now=datetime.now(timezone('Asia/Jerusalem')).isoformat()
        rows.append([job['source'],job['title'],job['link'],now,'Applied'])
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body={'values':rows}
        ).execute()
        await send_telegram(f"🔔 Pipeline finished: logged {len(rows)} jobs to '{tab}'.")
    except Exception as e:
        logger.error(f"Append to '{tab}' failed: {e}")
        await send_telegram(f"⚠️ Pipeline finished but failed to log to sheet '{tab}': {e}")

async def job_pipeline():
    await send_telegram(f"🔔 Started at {datetime.now().isoformat()}")
    jobs=fetch_jobs()
    relevant=filter_relevant(jobs)
    await apply_and_log(jobs,relevant)

if __name__=='__main__':
    asyncio.run(job_pipeline())
