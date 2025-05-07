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
# Requires env vars: OPENAI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
# GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT, GMAIL_USERNAME, GMAIL_PASSWORD,
# SERVICE_ACCOUNT_JSON_B64, GOOGLE_SHEETS_ID, EMAIL_FROM

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

openai.api_key = os.getenv('OPENAI_API_KEY')
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Decode service-account from Base64 env
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64')
service_account_info = json.loads(base64.b64decode(b64))
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
sheets_service = build('sheets', 'v4', credentials=creds)
drive_service = build('drive', 'v3', credentials=creds)
spreadsheet_id = os.getenv('GOOGLE_SHEETS_ID')

JOB_SOURCES = [
    {'name': 'AllJobs', 'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product+Manager&region=Center'}
]

def fetch_jobs() -> List[Dict]:
    jobs = []
    for source in JOB_SOURCES:
        resp = requests.get(source['url'])
        soup = BeautifulSoup(resp.text, 'html.parser')
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
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        if 'yes' in resp.choices[0].message.content.lower():
            relevant.append(job)
    logger.info(f"Filtered to {len(relevant)} relevant jobs")
    return relevant

def send_telegram(message: str):
    try:
        telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

def send_email(subject: str, body: str, attachments: List[str]=None):
    msg = MIMEMultipart()
    msg['From'] = os.getenv('EMAIL_FROM')
    msg['To'] = os.getenv('GMAIL_USERNAME')
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    for filepath in attachments or []:
        part = MIMEBase('application', 'octet-stream')
        with open(filepath, 'rb') as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(filepath)}"')
        msg.attach(part)
    with smtplib.SMTP(os.getenv('GMAIL_SMTP_SERVER'), int(os.getenv('GMAIL_SMTP_PORT'))) as server:
        server.starttls()
        server.login(os.getenv('GMAIL_USERNAME'), os.getenv('GMAIL_PASSWORD'))
        server.send_message(msg)
        logger.info("Email sent")

def generate_documents(job: Dict) -> Dict[str, str]:
    prompt = f"Tailor a resume and cover letter for '{job['title']}' at {job['link']}."
    resp = openai.ChatCompletion.create(model="gpt-4", messages=[{"role":"user","content":prompt}])
    segments = resp.choices[0].message.content.split('---')
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    resume, cover = segments[0].strip(), segments[1].strip() if len(segments)>1 else ''
    res_file, cov_file = f"resume_{ts}.txt", f"cover_{ts}.txt"
    open(res_file,'w').write(resume)
    open(cov_file,'w').write(cover)
    return {'resume':res_file,'cover':cov_file}

def apply_to_job(job: Dict, docs: Dict[str, str]):
    subject = f"App for {job['title']}"
    send_email(subject, f"Please find attached.", [docs['resume'], docs['cover']])
    now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range='Applications!A:D',
        valueInputOption='RAW',
        body={'values':[[job['title'], job['link'], now, 'Applied']]},
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
    send_telegram("🔔 Pipeline complete.")

if __name__ == '__main__':
    job_pipeline()
