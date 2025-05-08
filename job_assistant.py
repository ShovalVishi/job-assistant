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

# ------------------ CONFIGURATION ------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# HTTP headers for web scraping
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/115.0.0.0 Safari/537.36'
}

# OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Telegram
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
telegram_bot = Bot(token=TELEGRAM_TOKEN)

# Google credentials
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64', '')
service_account_info = json.loads(base64.b64decode(b64)) if b64 else {}
creds = service_account.Credentials.from_service_account_info(
    service_account_info,
    scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
) if service_account_info else None

sheets_service = build('sheets', 'v4', credentials=creds) if creds else None
drive_service = build('drive', 'v3', credentials=creds) if creds else None

SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_ID')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')
TARGET_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Applications')

# Job sources
JOB_SOURCES = [
    {'name': 'LinkedIn', 'url': 'https://www.linkedin.com/jobs/search?keywords=Product%20Manager&location=Central%20Israel'},
    {'name': 'AllJobs',   'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product%20Manager&region=Center'},
    {'name': 'Drushim',   'url': 'https://www.drushim.co.il/jobs/?q=Product%20Manager&loc=Center'},
    {'name': 'Indeed',    'url': 'https://il.indeed.com/jobs?q=Product+Manager&l=Central+Israel'},
    {'name': 'Glassdoor', 'url': 'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'},
]

async def send_telegram(message: str):
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Telegram sent: {message}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def fetch_jobs() -> List[Dict]:
    jobs: List[Dict] = []
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']} at {src['url']}")
        try:
            response = requests.get(src['url'], headers=HEADERS, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            before = len(jobs)
            if src['name'] == 'LinkedIn':
                for card in soup.select('ul.jobs-search__results-list li'):
                    a = card.select_one('a.base-card__full-link')
                    t = card.select_one('h3.base-search-card__title')
                    if a and t:
                        jobs.append({'source': 'LinkedIn', 'title': t.text.strip(), 'link': a['href']})
            elif src['name'] == 'AllJobs':
                for card in soup.select('.search-result-item'):
                    t = card.select_one('h3.job-title')
                    a = card.select_one('a.gif-link') or card.select_one('a')
                    if t and a and a.has_attr('href'):
                        href = a['href']
                        if not href.startswith('http'):
                            href = f"https://www.alljobs.co.il{href}"
                        jobs.append({'source': 'AllJobs', 'title': t.text.strip(), 'link': href})
            elif src['name'] == 'Drushim':
                for item in soup.select('.job-list__item a.job-list__link'):
                    href = item['href']
                    if not href.startswith('http'):
                        href = f"https://www.drushim.co.il{href}"
                    jobs.append({'source': 'Drushim', 'title': item.text.strip(), 'link': href})
            elif src['name'] == 'Indeed':
                for tag in soup.select('a.tapItem'):
                    t = tag.select_one('h2.jobTitle')
                    href = tag.get('href', '')
                    if t and href:
                        link = f"https://il.indeed.com{href}" if href.startswith('/') else href
                        jobs.append({'source': 'Indeed', 'title': t.text.strip(), 'link': link})
            elif src['name'] == 'Glassdoor':
                for tag in soup.select('li.react-job-listing'):
                    link_tag = tag.select_one('a.job-link') or tag.select_one('a.jobLink')
                    title_tag = tag.select_one('a > span')
                    if link_tag and link_tag.has_attr('href') and title_tag:
                        href = link_tag['href']
                        if not href.startswith('http'):
                            href = f"https://www.glassdoor.co.il{href}"
                        jobs.append({'source': 'Glassdoor', 'title': title_tag.text.strip(), 'link': href})
            count = len(jobs) - before
            logger.info(f"{src['name']}: found {count} jobs")
        except Exception as e:
            logger.error(f"Failed to scrape {src['name']}: {e}")
    logger.info(f"Total jobs fetched: {len(jobs)}")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    relevant: List[Dict] = []
    for job in jobs:
        try:
            prompt = (
                f"Job title: {job['title']}\n"
                f"Job link: {job['link']}\n"
                "Location: within 1-hour drive of Netanya, Israel\n"
                "Salary: around 25,000 ILS\n"
                "Respond with 'yes' or 'no' only: Is this relevant?"
            )
            resp = openai.ChatCompletion.create(  # Use correct ChatCompletion
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}]
            )
            content = resp.choices[0].message.content.lower()
            if 'yes' in content:
                relevant.append(job)
        except Exception:
            relevant.append(job)
    logger.info(f"Relevant jobs: {len(relevant)} / {len(jobs)}")
    return relevant

async def apply_and_log(jobs: List[Dict], relevant: List[Dict]):
    # determine sheet tab
    try:
        meta = sheets_service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        sheets = [s['properties']['title'] for s in meta.get('sheets', [])]
    except Exception as e:
        await send_telegram(f"Failed to fetch sheets metadata: {e}")
        return

    tab = TARGET_SHEET_NAME if TARGET_SHEET_NAME in sheets else sheets[0]
    logger.info(f"Using sheet tab '{tab}'")

    rows = []
    for job in relevant:
        now = datetime.now(timezone('Asia/Jerusalem')).isoformat()
        rows.append([job['source'], job['title'], job['link'], now, 'Applied'])

    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{tab}!A:E",
            valueInputOption='RAW',
            body={'values': rows}
        ).execute()
        await send_telegram(f"🔔 Pipeline finished: logged {len(rows)} jobs to '{tab}'.")
    except HttpError as e:
        await send_telegram(f"⚠️ Sheets append failed ({e.resp.status}): {e.content}")

async def job_pipeline():
    await send_telegram(f"🔔 Pipeline started at {datetime.now().isoformat()}")
    jobs = fetch_jobs()
    relevant = filter_relevant(jobs)
    await apply_and_log(jobs, relevant)

if __name__ == '__main__':
    asyncio.run(job_pipeline())
