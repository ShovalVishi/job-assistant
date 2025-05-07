import os
import json
import base64
import logging
from datetime import datetime
from typing import List, Dict

import openai
import requests
from bs4 import BeautifulSoup

from telegram import Bot
from telegram.error import TelegramError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import smtplib
from pytz import timezone

# ======================= CONFIGURATION =======================
# Env vars: OPENAI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
# GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT, GMAIL_USERNAME, GMAIL_PASSWORD,
# SERVICE_ACCOUNT_JSON_B64, GOOGLE_SHEETS_ID, EMAIL_FROM

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize clients
openai.api_key = os.getenv('OPENAI_API_KEY')
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Decode service account
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64')
info = json.loads(base64.b64decode(b64))
creds = service_account.Credentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
sheets_service = build('sheets', 'v4', credentials=creds)
drive_service = build('drive', 'v3', credentials=creds)
spreadsheet_id = os.getenv('GOOGLE_SHEETS_ID')

# ======================= JOB LOGIC =======================
JOB_SOURCES = [
    {'name': 'AllJobs', 'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product+Manager&region=Center'}
]

def fetch_jobs() -> List[Dict]:
    jobs = []
    for src in JOB_SOURCES:
        res = requests.get(src['url'])
        soup = BeautifulSoup(res.text, 'html.parser')
        for tag in soup.select('.job-card'):
            title = tag.select_one('.job-title').get_text(strip=True)
            link = tag.select_one('a')['href']
            jobs.append({'title': title, 'link': link})
    logger.info(f"Fetched {len(jobs)} jobs")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant = []
    for job in jobs:
        prompt = (f"Job title: {job['title']}\n"
                  "Location: within 1 hour drive of Netanya, Israel\n"
                  "Salary: around 25000 ILS\n"
                  "Is this role relevant? (yes/no)")
        resp = openai.ChatCompletion.create(model="gpt-4", messages=[{"role":"user","content":prompt}])
        if 'yes' in resp.choices[0].message.content.lower():
            relevant.append(job)
    logger.info(f"Filtered to {len(relevant)} relevant jobs")
    return relevant

def send_telegram(msg: str):
    try:
        telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

def send_email(subject: str, body: str, files: List[str] = None):
    msg = MIMEMultipart()
    msg['From'] = os.getenv('EMAIL_FROM')
    msg['To'] = os.getenv('GMAIL_USERNAME')
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    for fpath in files or []:
        part = MIMEBase('application','octet-stream')
        with open(fpath,'rb') as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(fpath)}"')
        msg.attach(part)
    with smtplib.SMTP(os.getenv('GMAIL_SMTP_SERVER'), int(os.getenv('GMAIL_SMTP_PORT'))) as s:
        s.starttls()
        s.login(os.getenv('GMAIL_USERNAME'), os.getenv('GMAIL_PASSWORD'))
        s.send_message(msg)
        logger.info("Email sent")

def generate_documents(job: Dict) -> Dict[str,str]:
    prompt = f"Tailor a resume and cover letter for '{job['title']}' at {job['link']}."
    resp = openai.ChatCompletion.create(model="gpt-4", messages=[{"role":"user","content":prompt}])
    parts = resp.choices[0].message.content.strip().split('---')
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    res_txt = parts[0].strip()
    cover_txt = parts[1].strip() if len(parts)>1 else ''
    res_file = f"resume_{ts}.txt"
    cover_file = f"cover_{ts}.txt"
    with open(res_file,'w') as f: f.write(res_txt)
    with open(cover_file,'w') as f: f.write(cover_txt)
    return {'resume':res_file,'cover':cover_file}

def apply_to_job(job: Dict, docs: Dict[str,str]):
    send_email(f"Application: {job['title']}", "Please see attachments.", [docs['resume'], docs['cover']])
    now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range='Applications!A:D',
        valueInputOption='RAW', body={'values':[[job['title'], job['link'], now, 'Applied']]}
    ).execute()
    logger.info(f"Logged {job['title']}")
    send_telegram(f"Completed application for {job['title']}")

def job_pipeline():
    send_telegram(f"🔔 Pipeline start at {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    for job in relevant:
        docs = generate_documents(job)
        apply_to_job(job, docs)
    send_telegram(f"🔔 Pipeline complete at {datetime.now().isoformat()}")

if __name__=="__main__":
    job_pipeline()
