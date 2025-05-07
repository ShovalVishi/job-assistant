import os
import logging
from datetime import datetime
from typing import List, Dict

import openai
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone

from telegram import Bot
from telegram.error import TelegramError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import smtplib

# ======================= CONFIGURATION =======================
# Environment variables required:
# OPENAI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
# GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT, GMAIL_USERNAME, GMAIL_PASSWORD,
# GOOGLE_CREDENTIALS_JSON (path), GOOGLE_SHEETS_ID,
# EMAIL_FROM

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize API clients
openai.api_key = os.getenv('OPENAI_API_KEY')
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file(
    os.getenv('GOOGLE_CREDENTIALS_JSON'), scopes=SCOPES)
sheets_service = build('sheets', 'v4', credentials=creds)
drive_service = build('drive', 'v3', credentials=creds)
spreadsheet_id = os.getenv('GOOGLE_SHEETS_ID')

# ======================= JOB AGGREGATOR =======================
JOB_SOURCES = [
    {'name': 'AllJobs', 'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product+Manager&region=Center'}
    # Add other sources as needed
]

def fetch_jobs() -> List[Dict]:
    jobs = []
    for source in JOB_SOURCES:
        resp = requests.get(source['url'])
        soup = BeautifulSoup(resp.text, 'html.parser')
        # TODO: parse listings according to site structure
        for tag in soup.select('.job-card'):
            title = tag.select_one('.job-title').get_text(strip=True)
            link = tag.select_one('a')['href']
            jobs.append({'source': source['name'], 'title': title, 'link': link})
    logger.info(f"Fetched {len(jobs)} jobs")
    return jobs

# ======================= RELEVANCE FILTER =======================
def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant = []
    for job in jobs:
        prompt = (
            f"Job title: {job['title']}\n"
            "Location: within 1 hour drive of Netanya, Israel\n"
            "Salary: around 25000 ILS\n"
            "Is this role relevant? (yes/no)"
        )
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        answer = resp.choices[0].message.content.strip().lower()
        if 'yes' in answer:
            relevant.append(job)
    logger.info(f"Filtered down to {len(relevant)} relevant jobs")
    return relevant

# ======================= NOTIFICATIONS =======================
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
        logger.info("Email sent successfully")

# ======================= DOCUMENT GENERATION =======================
def generate_documents(job: Dict) -> Dict[str, str]:
    prompt = (
        f"Tailor a professional resume and cover letter for Shoval applying to '{job['title']}' at {job['link']}."
    )
    resp = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    content = resp.choices[0].message.content.strip().split('---')
    resume_text = content[0].strip()
    cover_text = content[1].strip() if len(content) > 1 else ''
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    resume_file = f"resume_{timestamp}.txt"
    cover_file = f"cover_{timestamp}.txt"
    with open(resume_file, 'w') as f:
        f.write(resume_text)
    with open(cover_file, 'w') as f:
        f.write(cover_text)
    return {'resume': resume_file, 'cover': cover_file}

# ======================= APPLICATION & LOGGING =======================
def apply_to_job(job: Dict, docs: Dict[str, str]):
    subject = f"Application for {job['title']}"
    body = f"Dear Hiring Team,\n\nPlease find attached my resume and cover letter for the {job['title']} role.\n\nBest,\nShoval"
    send_email(subject, body, attachments=[docs['resume'], docs['cover']])
    sheet = sheets_service.spreadsheets()
    now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
    values = [[job['title'], job['link'], now, 'Applied']]
    sheet.values().append(
        spreadsheetId=spreadsheet_id,
        range='Applications!A:D',
        valueInputOption='RAW',
        body={'values': values}
    ).execute()
    logger.info(f"Logged application for {job['title']}")

# ======================= MAIN WORKFLOW =======================
def job_pipeline():
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    for job in relevant:
        msg = f"New match: {job['title']} at {job['link']}. Generate résumé? Reply YES to proceed."
        send_telegram(msg)
        # input("Press Enter after replying YES...")  # disabled for headless runs
        docs = generate_documents(job)
        review_msg = f"Here are your tailored docs for {job['title']}. Review and reply APPROVE to send."
        send_telegram(review_msg)
        # input("Press Enter after replying APPROVE...")  # disabled for headless runs
        apply_to_job(job, docs)
    send_telegram("Job pipeline run complete.")

if __name__ == '__main__':
    scheduler = BlockingScheduler(timezone='Asia/Jerusalem')
    trigger = CronTrigger(hour='8,18', minute=0)
    scheduler.add_job(job_pipeline, trigger)
    logger.info("Scheduler started. Running at 08:00 and 18:00 Asia/Jerusalem.")
    scheduler.start()
