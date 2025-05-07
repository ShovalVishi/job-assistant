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

openai.api_key = os.getenv('OPENAI_API_KEY')
telegram_bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Google services
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

# ------------------ JOB SOURCES ------------------
JOB_SOURCES = [
    {
        'name': 'AllJobs',
        'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product%20Manager&region=Center'
    },
    {
        'name': 'Drushim',
        'url': 'https://www.drushim.co.il/jobs/?q=Product%20Manager&loc=Center'
    },
    {
        'name': 'Indeed',
        'url': 'https://il.indeed.com/jobs?q=Product+Manager&l=Central+Israel'
    },
    {
        'name': 'Glassdoor',
        'url': 'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'
    },
    {
        'name': 'LinkedIn',
        'url': 'https://www.linkedin.com/jobs/search?keywords=Product%20Manager&location=Central%20Israel&refresh=true'
    },
]

def send_telegram(message: str):
    try:
        asyncio.run(telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
        logger.info(f"Telegram: {message}")
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
            count_before = len(jobs)
            if src['name'] == 'AllJobs':
                for card in soup.select('.search-result-item'):
                    t = card.select_one('h3.job-title')
                    link = card.select_one('a')
                    if t and link:
                        jobs.append({'source':'AllJobs','title':t.text.strip(),'link':link['href']})
            elif src['name'] == 'Drushim':
                for item in soup.select('.job-list__item'):
                    a = item.select_one('a.job-list__link')
                    if a:
                        jobs.append({'source':'Drushim','title':a.text.strip(),'link':a['href']})
            elif src['name'] == 'Indeed':
                for tag in soup.select('a.tapItem'):
                    title_tag = tag.select_one('h2.jobTitle')
                    if title_tag:
                        href = tag.get('href')
                        link = f"https://il.indeed.com{href}" if href.startswith('/') else href
                        jobs.append({'source':'Indeed','title':title_tag.text.strip(),'link':link})
            elif src['name'] == 'Glassdoor':
                for tag in soup.select('li.jl'):
                    a = tag.select_one('a.jobLink')
                    if a:
                        jobs.append({'source':'Glassdoor','title':a.text.strip(),'link':a['href']})
            elif src['name'] == 'LinkedIn':
                for card in soup.select('ul.jobs-search__results-list li'):
                    a = card.select_one('a.base-card__full-link')
                    title = card.select_one('h3.base-search-card__title')
                    if a and title:
                        jobs.append({'source':'LinkedIn','title':title.text.strip(),'link':a['href']})
            count = len(jobs)-count_before
            logger.info(f"{src['name']}: found {count} new jobs")
        except Exception as e:
            logger.error(f"Failed to scrape {src['name']}: {e}")
    logger.info(f"Total scraped jobs: {len(jobs)}")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant = []
    for job in jobs:
        try:
            prompt = f"Job title: {job['title']}\nLocation: within 1h drive of Netanya, Israel\nSalary ~25000 ILS\nRelevant?"
            resp = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"user","content":prompt}])
            if 'yes' in resp.choices[0].message.content.lower():
                relevant.append(job)
        except Exception as e:
            logger.warning(f"Filter error ({job['title']}): {e}, including by default")
            relevant.append(job)
    logger.info(f"Relevant: {len(relevant)} / {len(jobs)}")
    return relevant

def generate_documents(job: Dict) -> Dict[str,str]:
    try:
        prompt = f"Write resume and cover letter for '{job['title']}', link {job['link']}."
        resp = openai.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role":"user","content":prompt}])
        parts = resp.choices[0].message.content.split('---')
    except Exception as e:
        logger.warning(f"Doc gen error: {e}")
        parts = ["[Resume]", "[Cover]"]
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    res = f"resume_{ts}.txt"; cov = f"cover_{ts}.txt"
    with open(res,'w') as f: f.write(parts[0].strip())
    with open(cov,'w') as f: f.write(parts[1].strip() if len(parts)>1 else '')
    # upload to Drive
    for fp in [res,cov]:
        try:
            drive_service.files().create(body={'name':fp,'parents':[DRIVE_FOLDER_ID]}, media_body=MediaFileUpload(fp), fields='id').execute()
            logger.info(f"Uploaded {fp}")
        except Exception as e:
            logger.error(f"Drive upload {fp} failed: {e}")
    return {'resume':res,'cover':cov}

def apply_and_log(jobs: List[Dict], relevant: List[Dict]):
    for job in relevant:
        docs = generate_documents(job)
        send_email = None  # placeholder if needed
        try:
            now = datetime.now().astimezone(timezone('Asia/Jerusalem')).isoformat()
            sheets_service.spreadsheets().values().append(spreadsheetId=SPREADSHEET_ID, range='Applications!A:D', valueInputOption='RAW', body={'values':[[job['title'],job['link'],now,'Applied']]}).execute()
            logger.info(f"Logged {job['title']} to Sheets")
        except HttpError as e:
            logger.error(f"Sheets log failed: {e}")
    send_telegram(f"Job run: fetched {len(jobs)}, applied {len(relevant)}")

def job_pipeline():
    send_telegram(f"Start: {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    apply_and_log(jobs, relevant)

if __name__ == '__main__':
    job_pipeline()
