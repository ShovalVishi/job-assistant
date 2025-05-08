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

# OpenAI API key
openai.api_key = os.getenv('OPENAI_API_KEY')

# Telegram Bot
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variables are not set.")
telegram_bot = Bot(token=TELEGRAM_TOKEN)

# Google credentials
b64 = os.getenv('SERVICE_ACCOUNT_JSON_B64', '')
if not b64:
    logger.error("SERVICE_ACCOUNT_JSON_B64 environment variable is not set.")
service_account_info = {}
try:
    service_account_info = json.loads(base64.b64decode(b64))
except (TypeError, json.JSONDecodeError) as e:
    logger.error(f"Failed to decode SERVICE_ACCOUNT_JSON_B64: {e}")

creds = None
if service_account_info:
    try:
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/spreadsheets',
                    'https://www.googleapis.com/auth/drive']
        )
    except Exception as e:
        logger.error(f"Failed to create credentials: {e}")

sheets_service = build('sheets', 'v4', credentials=creds) if creds else None
drive_service = build('drive', 'v3', credentials=creds) if creds else None

SPREADSHEET_ID = os.getenv('GOOGLE_SHEETS_ID')
DRIVE_FOLDER_ID = os.getenv('DRIVE_FOLDER_ID')

if not SPREADSHEET_ID:
    logger.warning("GOOGLE_SHEETS_ID environment variable is not set.")
if not DRIVE_FOLDER_ID:
    logger.warning("DRIVE_FOLDER_ID environment variable is not set.")

# ------------------ JOB SOURCES ------------------
JOB_SOURCES = [
    {'name': 'AllJobs',  'url': 'https://www.alljobs.co.il/SearchResultsGuest.aspx?keyword=Product%20Manager&region=Center'},
    {'name': 'Drushim',  'url': 'https://www.drushim.co.il/jobs/?q=Product%20Manager&loc=Center'},
    {'name': 'Indeed',   'url': 'https://il.indeed.com/jobs?q=Product+Manager&l=Central+Israel'},
    {'name': 'Glassdoor','url': 'https://www.glassdoor.co.il/Job/central-israel-Product-Manager-jobs-SRCH_IL.0,13_IS360_KO14,31.htm'},
    {'name': 'LinkedIn', 'url': 'https://www.linkedin.com/jobs/search?keywords=Product%20Manager&location=Central%20Israel'}
]

async def send_telegram(message: str):
    if not telegram_bot or not TELEGRAM_CHAT_ID:
        logger.error("Telegram bot not configured.")
        return
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logger.info(f"Telegram sent: {message}")
    except TelegramError as e:
        logger.error(f"Telegram API error: {e}")
    except Exception as e:
        logger.error(f"Unexpected send_telegram error: {e}")

def fetch_jobs() -> List[Dict]:
    jobs = []
    for src in JOB_SOURCES:
        logger.info(f"Scraping {src['name']} ({src['url']})")
        try:
            r = requests.get(src['url'], timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            before = len(jobs)
            # Example selector logic per site...
            if src['name'] == 'AllJobs':
                for card in soup.select('.search-result-item'):
                    title = card.select_one('h3.job-title')
                    link_tag = card.select_one('a.job-item-vacancy-title') or card.select_one('a')
                    if title and link_tag and link_tag.has_attr('href'):
                        href = link_tag['href']
                        if not href.startswith('http'):
                            href = f"https://www.alljobs.co.il{href}"
                        jobs.append({'source': 'AllJobs', 'title': title.text.strip(), 'link': href})
            elif src['name'] == 'Drushim':
                for item in soup.select('.job-list__item'):
                    a = item.select_one('a.job-list__link')
                    if a and a.has_attr('href'):
                        href = a['href']
                        if not href.startswith('http'):
                            href = f"https://www.drushim.co.il{href}"
                        jobs.append({'source': 'Drushim', 'title': a.text.strip(), 'link': href})
            elif src['name'] == 'Indeed':
                for tag in soup.select('a.tapItem'):
                    t = tag.select_one('h2.jobTitle')
                    href = tag.get('href', '')
                    if t and href:
                        full = f"https://il.indeed.com{href}" if href.startswith('/') else href
                        jobs.append({'source': 'Indeed', 'title': t.text.strip(), 'link': full})
            elif src['name'] == 'Glassdoor':
                for tag in soup.select('li.react-job-listing'):
                    link_tag = tag.select_one('a.job-link') or tag.select_one('a.jobLink')
                    title_tag = tag.select_one('a > span') or tag.select_one('.jobLink')
                    if link_tag and link_tag.has_attr('href') and title_tag:
                        href = link_tag['href']
                        if not href.startswith('http'):
                            href = f"https://www.glassdoor.co.il{href}"
                        jobs.append({'source': 'Glassdoor', 'title': title_tag.text.strip(), 'link': href})
            elif src['name'] == 'LinkedIn':
                for card in soup.select('ul.jobs-search__results-list li'):
                    a = card.select_one('a.base-card__full-link') or card.select_one('a.job-card-list__title')
                    t = card.select_one('h3.base-search-card__title') or card.select_one('span.sr-only')
                    if a and a.has_attr('href') and t:
                        jobs.append({'source': 'LinkedIn', 'title': t.text.strip(), 'link': a['href']})
            count = len(jobs) - before
            logger.info(f"{src['name']}: found {count} jobs")
        except Exception as e:
            logger.error(f"Error scraping {src['name']}: {e}")
    logger.info(f"Total jobs fetched: {len(jobs)}")
    return jobs

def filter_relevant(jobs: List[Dict]) -> List[Dict]:
    if not openai.api_key:
        logger.error("OpenAI API key not set.")
        return jobs
    relevant = []
    for job in jobs:
        try:
            prompt = (
                f"Job title: {job['title']}
"
                f"Job link: {job['link']}
"
                "Location: within 1-hour drive of Netanya, Israel
"
                "Salary: around 25,000 ILS
"
                "Respond with 'yes' or 'no' only: Is this relevant?"
            )
            resp = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}]
            )
            if 'yes' in resp.choices[0].message.content.lower():
                relevant.append(job)
        except Exception as e:
            logger.warning(f"Filter error for '{job['title']}': {e}, including by default")
            relevant.append(job)
    logger.info(f"Relevant jobs: {len(relevant)} / {len(jobs)}")
    return relevant

def generate_documents(job: Dict) -> Dict[str, str]:
    resume_name = cover_name = ""
    if not openai.api_key:
        logger.error("OpenAI API key not set.")
        return {'resume': resume_name, 'cover': cover_name}
    try:
        prompt = (
            f"Create a concise resume and cover letter for '{job['title']}' "
            f"(apply here: {job['link']}). Focus on product/project management "
            "and business development achievements."
        )
        resp = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        content = resp.choices[0].message.content
        parts = content.split('---', 1)
        resume_text = parts[0].strip()
        cover_text = parts[1].strip() if len(parts) > 1 else "[Cover letter missing]"
    except Exception as e:
        logger.warning(f"Doc gen error for '{job['title']}': {e}")
        resume_text = f"[Resume generation failed for {job['title']}]"
        cover_text = f"[Cover letter generation failed for {job['title']}]"
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    safe = "".join(c if c.isalnum() else "_" for c in job['title'])[:30]
    resume_name = f"resume_{safe}_{ts}.txt"
    cover_name = f"cover_{safe}_{ts}.txt"
    with open(resume_name, 'w', encoding='utf-8') as f:
        f.write(resume_text)
    with open(cover_name, 'w', encoding='utf-8') as f:
        f.write(cover_text)
    logger.info(f"Generated files: {resume_name}, {cover_name}")
    if drive_service and DRIVE_FOLDER_ID:
        for fname in [resume_name, cover_name]:
            try:
                media = MediaFileUpload(fname, mimetype='text/plain')
                drive_service.files().create(
                    body={'name': fname, 'parents': [DRIVE_FOLDER_ID]},
                    media_body=media,
                    fields='id'
                ).execute()
                logger.info(f"Uploaded to Drive: {fname}")
            except HttpError as e:
                logger.error(f"Drive upload failed for {fname}: {e.resp.status} - {e.content}")
            except Exception as e:
                logger.error(f"Unexpected Drive error for {fname}: {e}")
            finally:
                try:
                    os.remove(fname)
                except Exception:
                    pass
    return {'resume': resume_name, 'cover': cover_name}

async def apply_and_log(jobs: List[Dict], relevant: List[Dict]):
    if not sheets_service or not SPREADSHEET_ID:
        await send_telegram(
            f"🔔 Pipeline finished: fetched {len(jobs)}, processed {len(relevant)} jobs, but Sheets not configured."
        )
        return
    rows = []
    for job in relevant:
        docs = generate_documents(job)
        now = datetime.now(timezone('Asia/Jerusalem')).isoformat()
        rows.append([job['title'], job['link'], now, 'Applied', docs['resume'], docs['cover']])
    if rows:
        try:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range='Applications!A:F',
                valueInputOption='USER_ENTERED',
                body={'values': rows}
            ).execute()
            logger.info(f"Appended {len(rows)} rows to Sheets")
        except HttpError as e:
            logger.error(f"Sheets append failed: {e.resp.status} - {e.content}")
        except Exception as e:
            logger.error(f"Unexpected Sheets error: {e}")
    await send_telegram(f"🔔 Pipeline finished: fetched {len(jobs)}, processed {len(relevant)} relevant jobs.")

async def job_pipeline():
    start = datetime.now().isoformat()
    await send_telegram(f"🔔 Pipeline started at {start}")
    jobs = fetch_jobs()
    if not jobs:
        await send_telegram("🔔 No jobs fetched; ending pipeline.")
        return
    relevant = filter_relevant(jobs)
    if not relevant:
        await send_telegram(f"🔔 Fetched {len(jobs)} jobs, none relevant; ending pipeline.")
        return
    await apply_and_log(jobs, relevant)
    end = datetime.now().isoformat()
    logger.info(f"Pipeline completed; duration: {end} - {start}")

if __name__ == '__main__':
    asyncio.run(job_pipeline())
