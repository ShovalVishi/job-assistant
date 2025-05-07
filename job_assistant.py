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

# Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Init APIs
openai.api_key = os.getenv('OPENAI_API_KEY')
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Google
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64')
service_account_info = json.loads(base64.b64decode(b64))
creds = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=['https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive']
)
sheets_service = build('sheets', 'v4', credentials=creds)
drive_service = build('drive', 'v3', credentials=creds)
SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_ID')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')

# Sources
JOB_SOURCES = [
    {'name': 'AllJobs', 'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product+Manager&region=Center'},
    {'name': 'Drushim', 'url': 'https://www.drushim.co.il/jobs/?q=Product+Manager&loc=Center'},
    {'name': 'Indeed', 'url': 'https://il.indeed.com/jobs?q=Product+Manager&l=Center'},
    {'name': 'Glassdoor', 'url': 'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'},
    {'name': 'LinkedIn', 'url': 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Product%20Manager&location=Tel%20Aviv'},
]

def fetch_jobs() -> List[Dict]:
    jobs = []
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']}...")
        resp = requests.get(src['url'])
        soup = BeautifulSoup(resp.text, 'html.parser')
        before = len(jobs)
        # scraping logic...
        count = len(jobs) - before
        logger.info(f"{src['name']}: found {count} jobs")
    logger.info(f"Fetched total {len(jobs)} jobs from {len(JOB_SOURCES)} sources")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    try:
        relevant = []
        # filtering logic...
        return relevant
    except Exception as e:
        logger.warning(f"OpenAI filter error ({e}), treating all {len(jobs)} jobs as relevant")
        return jobs

def send_telegram(message: str):
    try:
        asyncio.run(telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

def send_email(subject: str, body: str, attachments: List[str] = None):
    try:
        msg = MIMEMultipart()
        msg['From'] = os.getenv('EMAIL_FROM')
        msg['To'] = os.getenv('GMAIL_USERNAME')
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        port = int(os.getenv('GMAIL_SMTP_PORT') or '587')
        server = smtplib.SMTP()
        server.connect(os.getenv('GMAIL_SMTP_SERVER'), port)
        server.ehlo()
        server.starttls()
        server.ehlo()
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

def generate_documents(job: Dict) -> Dict[str, str]:
    # document generation logic...
    return {'resume': 'resume.txt', 'cover': 'cover.txt'}

def apply_and_summarize(jobs: List[Dict], relevant: List[Dict]):
    for job in relevant:
        docs = generate_documents(job)
        send_email(f"Application for {job['title']}", "Please find attached.", [docs['resume'], docs['cover']])
        try:
            now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range='Applications!A:D',
                valueInputOption='RAW',
                body={'values': [[job['title'], job['link'], now, 'Applied']]}
            ).execute()
            logger.info(f"Logged application for {job['title']}")
        except HttpError as e:
            logger.error(f"Failed to log to Google Sheets: {e}")
    total = len(jobs)
    sent = len(relevant)
    send_telegram(f"🔔 Pipeline finished: found {total} jobs, {sent} relevant and applied.")

def job_pipeline():
    send_telegram(f"🔔 Pipeline started at {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    apply_and_summarize(jobs, relevant)

if __name__ == '__main__':
    job_pipeline()
