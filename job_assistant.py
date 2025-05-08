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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI API key
openai.api_key = os.getenv('OPENAI_API_KEY')

# Telegram Bot
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Google credentials
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64', '')
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

# Job sources
JOB_SOURCES = [
    {'name': 'AllJobs',  'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product%20Manager&region=Center'},
    {'name': 'Drushim',  'url': 'https://www.drushim.co.il/jobs/?q=Product%20Manager&loc=Center'},
    {'name': 'Indeed',   'url': 'https://il.indeed.com/jobs?q=Product+Manager&l=Central+Israel'},
    {'name': 'Glassdoor','url': 'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'},
    {'name': 'LinkedIn', 'url': 'https://www.linkedin.com/jobs/search?keywords=Product%20Manager&location=Central%20Israel'}
]

def send_telegram(message: str):
    try:
        asyncio.run(telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
        logger.info(f"Telegram sent: {message}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fetch_jobs() -> List[Dict]:
    jobs = []
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']} ({src['url']})")
        try:
            r = requests.get(src['url'], timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            before = len(jobs)
            if src['name'] == 'AllJobs':
                for card in soup.select('.search-result-item'):
                    t = card.select_one('h3.job-title')
                    link = card.select_one('a')
                    if t and link:
                        jobs.append({'source': 'AllJobs', 'title': t.text.strip(), 'link': link['href']})
            elif src['name'] == 'Drushim':
                for item in soup.select('.job-list__item'):
                    a = item.select_one('a.job-list__link')
                    if a:
                        jobs.append({'source': 'Drushim', 'title': a.text.strip(), 'link': a['href']})
            elif src['name'] == 'Indeed':
                for tag in soup.select('a.tapItem'):
                    title_tag = tag.select_one('h2.jobTitle')
                    href = tag.get('href', '')
                    if title_tag:
                        link = f"https://il.indeed.com{href}" if href.startswith('/') else href
                        jobs.append({'source': 'Indeed', 'title': title_tag.text.strip(), 'link': link})
            elif src['name'] == 'Glassdoor':
                for tag in soup.select('li.jl'):
                    a = tag.select_one('a.jobLink')
                    if a:
                        jobs.append({'source': 'Glassdoor', 'title': a.text.strip(), 'link': a['href']})
            elif src['name'] == 'LinkedIn':
                for card in soup.select('ul.jobs-search__results-list li'):
                    a = card.select_one('a.base-card__full-link')
                    title = card.select_one('h3.base-search-card__title')
                    if a and title:
                        jobs.append({'source': 'LinkedIn', 'title': title.text.strip(), 'link': a['href']})
            count = len(jobs) - before
            logger.info(f"{src['name']}: found {count} jobs")
        except Exception as e:
            logger.error(f"Error scraping {src['name']}: {e}")
    logger.info(f"Total jobs fetched: {len(jobs)}")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant = []
    for job in jobs:
        try:
            prompt = (f"Job title: {job['title']}\nLocation: within 1h drive of Netanya\n"
                      "Salary ~25000 ILS\nIs this relevant? yes/no")
            resp = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role":"user","content":prompt}]
            )
            if 'yes' in resp.choices[0].message.content.lower():
                relevant.append(job)
        except Exception as e:
            logger.warning(f"Filter error {job['title']}: {e}, default include")
            relevant.append(job)
    logger.info(f"Relevant jobs: {len(relevant)}")
    return relevant

def generate_documents(job: Dict) -> Dict[str,str]:
    try:
        prompt = f"Create resume and cover letter for '{job['title']}', apply link: {job['link']}."
        resp = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}]
        )
        parts = resp.choices[0].message.content.split('---')
    except Exception as e:
        logger.warning(f"Doc gen error: {e}")
        parts = ["[Resume]", "[Cover letter]"]
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    res = f"resume_{ts}.txt"; cov = f"cover_{ts}.txt"
    with open(res,'w') as f: f.write(parts[0].strip())
    with open(cov,'w') as f: f.write(parts[1].strip() if len(parts)>1 else '')
    for fp in [res,cov]:
        try:
            drive_service.files().create(
                body={'name':fp,'parents':[DRIVE_FOLDER_ID]},
                media_body=MediaFileUpload(fp),
                fields='id'
            ).execute()
            logger.info(f"Uploaded {fp}")
        except Exception as e:
            logger.error(f"Drive upload {fp} failed: {e}")
    return {'resume':res,'cover':cov}

def apply_and_log(jobs: List[Dict], relevant: List[Dict]):
    rows = []
    for job in relevant:
        docs = generate_documents(job)
        now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
        rows.append([job['title'], job['link'], now, 'Applied'])
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Applications!A:D',
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()
        logger.info(f"Appended {len(rows)} to Sheets")
    except Exception as e:
        logger.error(f"Sheets append failed: {e}")
    send_telegram(f"🔔 Pipeline finished: fetched {len(jobs)}, applied {len(relevant)}")

def job_pipeline():
    send_telegram(f"🔔 Pipeline started at {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    apply_and_log(jobs, relevant)

if __name__ == '__main__':
    job_pipeline()
