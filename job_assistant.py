#!/usr/bin/env python3
"""
JobAssistant: Automatic job search, filtering, logging, interactive application with approval,
and enriched company info logging (feature #6).

Features:
  - Scrapes LinkedIn, AllJobs, Drushim, Indeed, Glassdoor
  - Filters by job titles, salary range, distance from home or train proximity
  - Logs only new jobs at the top of Applications sheet with detailed columns:
      A: Source
      B: Company Name
      C: Field of Activity
      D: Job Location
      E: Number of Employees
      F: Money Raised (ILS)
      G: Major Clients (JSON)
      H: Job Link
      I: Date Found
      J: Status (NEW/SUBMITTED)
      K: Resume File
      L: Cover File
      M: Response Status
  - Telegram summary of relevant new jobs
  - `/apply` command: generate résumé & cover letter drafts, upload to Drive, provide draft links
  - `/approve` command: upon your approval, mark applications as SUBMITTED in the sheet

Required env vars:
  OPENAI_API_KEY
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
  SERVICE_ACCOUNT_JSON_B64
  GOOGLE_SHEETS_ID
  GOOGLE_SHEET_CONFIG_TAB
  GOOGLE_SHEET_APP_TAB
  DRIVE_FOLDER_ID

Config tab params:
  sources_json        JSON array of {name, url} objects
  job_titles          JSON array of job title keywords
  salary_min_ils      minimum salary in ILS
  salary_max_ils      maximum salary in ILS
  location_radius_km  radius from home in km
  train_allowed       "true" or "false"
  train_radius_km     radius from train station in km
  check_hours         comma-separated hours (24h) for daily checks
"""
import os, json, base64, logging, re
from datetime import datetime
from typing import List, Dict, Tuple
import requests
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from pytz import timezone
import openai
from telegram import Bot, ParseMode, Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Home coordinates (Netanya)
HOME_COORDS = (32.3329, 34.8590)

# Initialize services
openai.api_key = os.getenv('OPENAI_API_KEY')
bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
TELEGRAM_CHAT_ID = int(os.getenv('TELEGRAM_CHAT_ID', '0'))
svc_info = json.loads(base64.b64decode(os.getenv('SERVICE_ACCOUNT_JSON_B64', '')))
creds = service_account.Credentials.from_service_account_info(
    svc_info,
    scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
)
sheets = build('sheets', 'v4', credentials=creds)
drive = build('drive', 'v3', credentials=creds)

# Geolocation
geolocator = Nominatim(user_agent='jobassistant')

# In-memory storage
pending_drafts: Dict[int, Dict] = {}
new_jobs_cache: List[Dict] = []

def read_config() -> Dict[str, str]:
    rows = sheets.spreadsheets().values().get(
        spreadsheetId=os.getenv('GOOGLE_SHEETS_ID'),
        range=f"{os.getenv('GOOGLE_SHEET_CONFIG_TAB', 'Config')}!A:B"
    ).execute().get('values', [])
    return {r[0]: r[1] for r in rows if len(r) >= 2}

def fetch_jobs(sources: List[Dict[str, str]]) -> List[Dict]:
    jobs = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    for src in sources:
        name, url = src['name'], src['url']
        try:
            logger.info(f"Scraping {name}")
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            if name == 'LinkedIn':
                for li in soup.select('ul.jobs-search__results-list li'):
                    a = li.select_one('a.base-card__full-link')
                    t = li.select_one('h3.base-search-card__title')
                    if a and t:
                        jobs.append({'source': name, 'title': t.text.strip(), 'link': a['href']})
            elif name == 'AllJobs':
                for c in soup.select('.search-result-item'):
                    t = c.select_one('h3.job-title')
                    l = c.select_one('a.job-item-vacancy-title')
                    if t and l and l.has_attr('href'):
                        href = l['href']
                        link = href if href.startswith('http') else f"https://www.alljobs.co.il{href}"
                        jobs.append({'source': name, 'title': t.text.strip(), 'link': link})
            elif name == 'Drushim':
                for i in soup.select('.job-list__item'):
                    a = i.select_one('a.job-list__link')
                    if a and a.has_attr('href'):
                        href = a['href']
                        link = href if href.startswith('http') else f"https://www.drushim.co.il{href}"
                        jobs.append({'source': name, 'title': a.text.strip(), 'link': link})
            elif name == 'Indeed':
                for tg in soup.select('a.tapItem'):
                    tt = tg.select_one('h2.jobTitle span[title]')
                    href = tg.get('href', '')
                    if tt and href:
                        link = href if href.startswith('http') else f"https://il.indeed.com{href}"
                        jobs.append({'source': name, 'title': tt.text.strip(), 'link': link})
            elif name == 'Glassdoor':
                for tg in soup.select('li.react-job-listing'):
                    a = tg.select_one('a.job-link')
                    ts = tg.select_one('a > span')
                    if a and ts and a.has_attr('href'):
                        href = a['href']
                        link = href if href.startswith('http') else f"https://www.glassdoor.com{href}"
                        jobs.append({'source': name, 'title': ts.text.strip(), 'link': link})
        except Exception as e:
            logger.error(f"Error scraping {name}: {e}")
    logger.info(f"Fetched {len(jobs)} jobs")
    return jobs

def get_job_details(link: str) -> Tuple[str, int]:
    try:
        r = requests.get(link, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        text = BeautifulSoup(r.text, 'html.parser').get_text(' ')
        loc_m = re.search(r'(?:Location|מיקום)[:\s]*([\w\s,\-]+)', text)
        sal_m = re.search(r'₪\s*([\d,]+)', text)
        location = loc_m.group(1).strip() if loc_m else ''
        salary = int(sal_m.group(1).replace(',', '')) if sal_m else None
        return location, salary
    except:
        return '', None

def get_company_info(link: str) -> Dict:
    try:
        prompt = (
            f"For the job posting URL {link}, extract the employer's company name, "
            "field of activity, number of employees, approximate total money raised in ILS, "
            "and list of major clients with locations. Respond with ONLY JSON."
        )
        resp = openai.ChatCompletion.create(model='gpt-3.5-turbo', messages=[
            {'role': 'user', 'content': prompt}
        ])
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.warning(f"Company info error for {link}: {e}")
        return {'company_name': '', 'field_of_activity': '', 'num_employees': '',
                'money_raised_ils': '', 'clients': []}

def record_new(jobs: List[Dict]) -> List[Dict]:
    tab = os.getenv('GOOGLE_SHEET_APP_TAB', 'Applications')
    exist = {r[0] for r in sheets.spreadsheets().values().get(
        spreadsheetId=os.getenv('GOOGLE_SHEETS_ID'), range=f"{tab}!H:H").execute().get('values', [])}
    new = [j for j in jobs if j['link'] not in exist]
    if not new:
        bot.send_message(TELEGRAM_CHAT_ID, "🔔 No new jobs.")
        return []
    meta = sheets.spreadsheets().get(spreadsheetId=os.getenv('GOOGLE_SHEETS_ID')).execute()
    sid = next(s['properties']['sheetId'] for s in meta['sheets']
               if s['properties']['title'] == tab)
    sheets.spreadsheets().batchUpdate(spreadsheetId=os.getenv('GOOGLE_SHEETS_ID'),
        body={'requests': [{
            'insertDimension': {
                'range': {'sheetId': sid, 'dimension': 'ROWS',
                          'startIndex': 1, 'endIndex': 1+len(new)},
                'inheritFromBefore': False
            }
        }]}).execute()
    now = datetime.now(timezone('Asia/Jerusalem')).isoformat()
    vals = []
    for j in new:
        loc, sal = get_job_details(j['link'])
        info = get_company_info(j['link'])
        vals.append([
            j['source'],
            info.get('company_name', ''),
            info.get('field_of_activity', ''),
            loc,
            info.get('num_employees', ''),
            info.get('money_raised_ils', ''),
            json.dumps(info.get('clients', [])),
            j['link'],
            now,
            'NEW', '', '', ''
        ])
    sheets.spreadsheets().values().update(
        spreadsheetId=os.getenv('GOOGLE_SHEETS_ID'),
        range=f"{tab}!A2:M{1+len(vals)}",
        valueInputOption='RAW', body={'values': vals}
    ).execute()
    global new_jobs_cache
    new_jobs_cache = new
    return new

def filter_jobs(jobs: List[Dict], cfg: Dict[str, str]) -> List[Dict]:
    titles = json.loads(cfg.get('job_titles', '[]'))
    mn = int(cfg.get('salary_min_ils', '0'))
    mx = int(cfg.get('salary_max_ils', '9999999'))
    loc_rad = float(cfg.get('location_radius_km', '50'))
    train_ok = cfg.get('train_allowed', 'false').lower() == 'true'
    train_rad = float(cfg.get('train_radius_km', '2'))
    out = []
    for j in jobs:
        if not any(t.lower() in j['title'].lower() for t in titles):
            continue
        loc, sal = get_job_details(j['link'])
        if sal and (sal < mn or sal > mx):
            continue
        dist = None
        if loc:
            try:
                geo = geolocator.geocode(loc)
                if geo:
                    dist = geodesic(HOME_COORDS, (geo.latitude, geo.longitude)).km
            except:
                dist = None
        if dist is not None and dist <= loc_rad:
            out.append(j)
        elif train_ok and dist is not None and dist <= train_rad:
            out.append(j)
    return out

async def draft_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /apply 1 2 3")
        return
    nums = [int(x) for x in args if x.isdigit()]
    sel = [new_jobs_cache[n-1] for n in nums if 1 <= n <= len(new_jobs_cache)]
    if not sel:
        await update.message.reply_text("No valid job numbers.")
        return
    drafts, lines = {}, []
    for idx, job in zip(nums, sel):
        prompt = f"Generate resume and cover letter for '{job['title']}' (link: {job['link']}). Separate with '---'."
        resp = openai.ChatCompletion.create(model='gpt-3.5-turbo', messages=[{'role':'user','content':prompt}])
        res_txt, cv_txt = resp.choices[0].message.content.split('---', 1)
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        safe = re.sub(r'[^0-9A-Za-z]', '_', job['title'])[:30]
        fn1, fn2 = f"resume_{safe}_{ts}.txt", f"cover_{safe}_{ts}.txt"
        open(fn1,'w').write(res_txt.strip()); open(fn2,'w').write(cv_txt.strip())
        def up(fn): 
            meta={'name':fn,'parents':[os.getenv('DRIVE_FOLDER_ID')]}
            m=MediaFileUpload(fn,mimetype='text/plain'); f=drive.files().create(body=meta,media_body=m,fields='id').execute()
            os.remove(fn); return f"https://drive.google.com/file/d/{f['id']}/view"
        link1, link2 = up(fn1), up(fn2)
        drafts[idx] = {'job': job, 'resume': link1, 'cover': link2}
        lines.append(f"{idx}. {job['title']}\nResume: {link1}\nCover: {link2}")
    global pending_drafts; pending_drafts = drafts
    await update.message.reply_text("🔖 Drafts:\n\n" + "\n\n".join(lines) + "\n\nWhen ready, /approve " + " ".join(map(str, drafts)))

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args=context.args; nums=[int(x) for x in args if x.isdigit()]
    sel=[pending_drafts.get(n) for n in nums if pending_drafts.get(n)]
    if not sel:
        await update.message.reply_text("No valid drafts to approve."); return
    tab=os.getenv('GOOGLE_SHEET_APP_TAB','Applications')
    vals=sheets.spreadsheets().values().get(spreadsheetId=os.getenv('GOOGLE_SHEETS_ID'),range=f"{tab}!H:H").execute().get('values',[])
    for entry in sel:
        job=entry['job']
        for i,row in enumerate(vals, start=2):
            if row and row[0]==job['link']:
                sheets.spreadsheets().values().update(
                    spreadsheetId=os.getenv('GOOGLE_SHEETS_ID'),
                    range=f"{tab}!K{i}:M{i}",
                    valueInputOption='RAW',
                    body={'values':[['SUBMITTED',entry['resume'],entry['cover']] ]}
                ).execute()
                break
    await update.message.reply_text(f"✅ Approved {len(sel)} jobs."); pending_drafts.clear()

async def scheduled_task():
    cfg=read_config()
    sources=json.loads(cfg.get('sources_json','[]'))
    jobs=fetch_jobs(sources)
    new=record_new(jobs)
    if not new: return
    relevant=filter_jobs(new, cfg)
    if not relevant:
        bot.send_message(TELEGRAM_CHAT_ID,f"🔔 Found {len(new)} new, none relevant.")
    else:
        lines=['🔔 Relevant new jobs:']
        for i,j in enumerate(relevant,1):
            lines.append(f"{i}. {j['title']} (<a href='{j['link']}'>link</a>)")
        bot.send_message(TELEGRAM_CHAT_ID,"\n".join(lines),parse_mode=ParseMode.HTML)
        global new_jobs_cache; new_jobs_cache=relevant

async def main():
    app=ApplicationBuilder().token(os.getenv('TELEGRAM_TOKEN')).build()
    app.add_handler(CommandHandler('apply',draft_command))
    app.add_handler(CommandHandler('approve',approve_command))
    cfg=read_config(); hours=[int(h) for h in cfg.get('check_hours','8,18').split(',')]
    sched=AsyncIOScheduler(); import asyncio
    for h in hours:
        sched.add_job(lambda: asyncio.create_task(scheduled_task()),'cron',hour=h,minute=0)
    sched.start(); await app.initialize(); await app.start(); await app.updater.start_polling(); await app.idle()

if __name__=='__main__':
    import asyncio; asyncio.run(main())
