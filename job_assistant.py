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

# ======================= CONFIGURATION =======================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Telegram
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Google credentials
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64')
service_account_info = json.loads(base64.b64decode(b64 or ''))
creds = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=['https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive']
)
sheets_service = build('sheets', 'v4', credentials=creds)
drive_service = build('drive', 'v3', credentials=creds)
SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_ID')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')

# Job sources
JOB_SOURCES = [
    {'name': 'AllJobs', 'url': 'https://www.alljobs.co.il/...'},
    {'name': 'Drushim', 'url': 'https://www.drushim.co.il/...'},
    {'name': 'Indeed', 'url': 'https://il.indeed.com/...'},
    {'name': 'Glassdoor', 'url': 'https://www.glassdoor.co.il/...'},
    {'name': 'LinkedIn', 'url': 'https://www.linkedin.com/...'}
]

def send_telegram(message: str):
    try:
        asyncio.run(telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
        logger.info(f"Sent Telegram: {message}")
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

def send_email(subject: str, body: str, attachments: List[str] = None):
    try:
        msg = MIMEMultipart()
        msg['From'] = os.getenv('EMAIL_FROM')
        msg['To'] = os.getenv('GMAIL_USERNAME')
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        port = int(os.getenv('GMAIL_SMTP_PORT') or '587')
        server = smtplib.SMTP(os.getenv('GMAIL_SMTP_SERVER'), port)
        server.ehlo(); server.starttls(); server.ehlo()
        server.login(os.getenv('GMAIL_USERNAME'), os.getenv('GMAIL_PASSWORD'))
        for fp in attachments or []:
            part = MIMEBase('application', 'octet-stream')
            with open(fp, 'rb') as f:
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(fp)}"')
            msg.attach(part)
        server.send_message(msg)
        server.quit()
        logger.info("Email sent successfully")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

def fetch_jobs() -> List[Dict]:
    jobs = []
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']} at {src['url']}")
        try:
            resp = requests.get(src['url'], timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            # sample selector logic...
        except Exception as e:
            logger.error(f"Error fetching from {src['name']}: {e}")
    logger.info(f"Total jobs fetched: {len(jobs)}")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant = []
    for job in jobs:
        try:
            prompt = f"Job: {job['title']}..."
            resp = openai.chat.completions.create(model="gpt-3.5-turbo",
                                                  messages=[{"role":"user","content":prompt}])
            if 'yes' in resp.choices[0].message.content.lower():
                relevant.append(job)
        except Exception as e:
            logger.warning(f"Filter error on {job['title']}: {e}")
            relevant.append(job)
    logger.info(f"Relevant jobs: {len(relevant)}")
    return relevant

def generate_and_upload(job: Dict) -> Dict[str,str]:
    # ... generate docs ...
    docs = {'resume': 'resume.txt', 'cover': 'cover.txt'}
    # upload
    for name, path in docs.items():
        try:
            if os.path.exists(path):
                drive_service.files().create(
                    body={'name': path, 'parents':[DRIVE_FOLDER_ID]},
                    media_body=MediaFileUpload(path),
                    fields='id'
                ).execute()
                logger.info(f"Uploaded {path} to Drive")
        except Exception as e:
            logger.error(f"Drive upload error for {path}: {e}")
    return docs

def apply_and_log(jobs: List[Dict], relevant: List[Dict]):
    for job in relevant:
        docs = generate_and_upload(job)
        send_email(f"Apply: {job['title']}", "See attachments", [docs['resume'], docs['cover']])
        try:
            now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range="Applications!A:D",
                valueInputOption="RAW",
                body={'values': [[job['title'], job.get('link'), now, 'Applied']]}
            ).execute()
            logger.info(f"Logged {job['title']} to Sheets")
        except HttpError as e:
            logger.error(f"Sheets error: {e}")
    total = len(jobs); sent = len(relevant)
    try:
        send_telegram(f"🔔 Pipeline done: {total} jobs fetched, {sent} applied.")
    except Exception as e:
        logger.error(f"Summary Telegram error: {e}")

def job_pipeline():
    send_telegram(f"🔔 Starting at {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    apply_and_log(jobs, relevant)

if __name__ == "__main__":
    job_pipeline()
