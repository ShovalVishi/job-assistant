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
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import smtplib

# ======================= CONFIGURATION =======================
# Required environment variables:
# OPENAI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
# GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT, GMAIL_USERNAME, GMAIL_PASSWORD,
# SERVICE_ACCOUNT_JSON_B64, GOOGLE_SHEETS_ID, EMAIL_FROM

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize OpenAI and Telegram
openai.api_key = os.getenv('OPENAI_API_KEY')
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Decode and load Google service account credentials from Base64
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64')
service_account_info = json.loads(base64.b64decode(b64))
creds = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
)
sheets_service = build('sheets', 'v4', credentials=creds)
drive_service = build('drive', 'v3', credentials=creds)
spreadsheet_id = os.getenv('GOOGLE_SHEETS_ID')

# ======================= JOB SOURCES =======================
JOB_SOURCES = [
    {
        'name': 'AllJobs',
        'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product+Manager&region=Center'
    },
    {
        'name': 'Drushim',
        'url': 'https://www.drushim.co.il/jobs/?q=Product+Manager&loc=Center'
    },
    {
        'name': 'Indeed',
        'url': 'https://il.indeed.com/jobs?q=Product+Manager&l=Center'
    },
    {
        'name': 'Glassdoor',
        'url': 'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'
    },
    {
        'name': 'LinkedIn',
        'url': 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=Product%20Manager&location=Tel%20Aviv'
    }
]

# ======================= SCRAPE & LOGGING =======================
def fetch_jobs() -> List[Dict]:
    jobs: List[Dict] = []
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']}...")
        resp = requests.get(src['url'])
        soup = BeautifulSoup(resp.text, 'html.parser')
        before = len(jobs)
        if src['name'] == 'AllJobs':
            for tag in soup.select('.job-card'):
                title = tag.select_one('.job-title').get_text(strip=True)
                link = tag.select_one('a')['href']
                jobs.append({'source': src['name'], 'title': title, 'link': link})
        elif src['name'] == 'Drushim':
            for tag in soup.select('div.job-list__item'):
                a = tag.select_one('a.job-list__link')
                if a:
                    jobs.append({'source': src['name'], 'title': a.get_text(strip=True), 'link': a['href']})
        elif src['name'] == 'Indeed':
            for tag in soup.select('a.tapItem'):
                title_tag = tag.select_one('h2.jobTitle') or tag.select_one('span.jobTitle')
                title = title_tag.get_text(strip=True) if title_tag else ''
                href = tag.get('href', '')
                link = f'https://il.indeed.com{href}' if href.startswith('/') else href
                jobs.append({'source': src['name'], 'title': title, 'link': link})
        elif src['name'] == 'Glassdoor':
            for tag in soup.select('li.jl'):
                a = tag.select_one('a.jobLink')
                if a:
                    jobs.append({'source': src['name'], 'title': a.get_text(strip=True), 'link': a['href']})
        elif src['name'] == 'LinkedIn':
            for tag in soup.select('a.base-card__full-link'):
                title = tag.get_text(strip=True)
                link = tag['href']
                jobs.append({'source': src['name'], 'title': title, 'link': link})
        count = len(jobs) - before
        logger.info(f"{src['name']}: found {count} jobs")
    logger.info(f"Fetched total {len(jobs)} jobs")
    return jobs

# ======================= FILTER RELEVANCE =======================
def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant: List[Dict] = []
    for job in jobs:
        prompt = (
            f"Job title: {job['title']}\n"
            "Location: within 1 hour drive of Netanya, Israel\n"
            "Salary: around 25000 ILS\n"
            "Is this role relevant? (yes/no)"
        )
        resp = openai.chat.completions.create(
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
        asyncio.run(telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")

def send_email(subject: str, body: str, attachments: List[str] = None):
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
    with smtplib.SMTP(os.getenv('GMAIL_SMTP_SERVER'), int(os.getenv('GMAIL_SMTP_PORT'))) as s:
        s.starttls()
        s.login(os.getenv('GMAIL_USERNAME'), os.getenv('GMAIL_PASSWORD'))
        s.send_message(msg)
        logger.info("Email sent successfully")

# ======================= DOCUMENT GENERATION =======================
def generate_documents(job: Dict) -> Dict[str, str]:
    prompt = f"Tailor a professional resume and cover letter for Shoval applying to '{job['title']}' at {job['link']}."
    resp = openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    content = resp.choices[0].message.content.strip().split('---')
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    resume_path = f"resume_{ts}.txt"
    cover_path = f"cover_{ts}.txt"
    with open(resume_path, 'w') as f:
        f.write(content[0].strip())
    if len(content) > 1:
        with open(cover_path, 'w') as f:
            f.write(content[1].strip())
    return {'resume': resume_path, 'cover': cover_path}

# ======================= APPLY & LOG =======================
def apply_to_job(job: Dict, docs: Dict[str, str]):
    send_email(f"Application for {job['title']}", "Please find attached.", [docs['resume'], docs['cover']])
    now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range='Applications!A:D',
        valueInputOption='RAW',
        body={'values': [[job['title'], job['link'], now, 'Applied']]}
    ).execute()
    logger.info(f"Logged application for {job['title']}")
    send_telegram(f"✅ Completed application for {job['title']}")

# ======================= MAIN WORKFLOW =======================
def job_pipeline():
    send_telegram(f"🔔 Pipeline started at {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    for job in relevant:
        docs = generate_documents(job)
        apply_to_job(job, docs)
    send_telegram(f"🔔 Pipeline completed at {datetime.now().isoformat()}")

if __name__ == '__main__':
    job_pipeline()
