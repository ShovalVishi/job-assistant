#!/usr/bin/env python3
import os, json, base64, logging, asyncio
from email.mime.text import MIMEText
import base64 as b64lib

import openai
from google.oauth2 import service_account
from googleapiclient.discovery import build

# CONFIG
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
openai.api_key = os.getenv("OPENAI_API_KEY")
B64 = os.getenv("SERVICE_ACCOUNT_JSON_B64","")
DELEGATE = os.getenv("GMAIL_DELEGATE_EMAIL","")
svc_info = json.loads(base64.b64decode(B64))
creds = service_account.Credentials.from_service_account_info(
    svc_info, scopes=[
       "https://www.googleapis.com/auth/gmail.readonly",
       "https://www.googleapis.com/auth/gmail.compose",
       "https://www.googleapis.com/auth/spreadsheets"
    ], subject=DELEGATE
)
gmail = build("gmail","v1",creds)
sheets = build("sheets","v4",creds)
SHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
APP_TAB  = os.getenv("GOOGLE_SHEET_APP_TAB","Applications")

async def classify(body:str)->str:
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":
            "Classify email as positive or negative:\n\n"+body}]
    )
    return resp.choices[0].message.content.strip().lower()

def list_msgs():
    return gmail.users().messages().list(
        userId="me", labelIds=["INBOX"], q="subject:Re: is:unread"
    ).execute().get("messages",[])

def get_msg(mid):
    m = gmail.users().messages().get(userId="me",id=mid,format="full").execute()
    hdr = {h["name"]:h["value"] for h in m["payload"]["headers"]}
    body = ""
    for p in m["payload"].get("parts",[]):
        if p["mimeType"]=="text/plain":
            body=b64lib.urlsafe_b64decode(p["body"]["data"]).decode()
    return {"id":mid,"from":hdr.get("From",""),"subject":hdr.get("Subject",""),"body":body}

async def process():
    msgs = list_msgs()
    for m in msgs:
        d = get_msg(m["id"])
        c = await classify(d["body"])
        # find row and update col F
        vals = sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,range=f"{APP_TAB}!C:C"
        ).execute().get("values",[])
        idx = next((i for i,row in enumerate(vals) if row[0] in d["subject"]),None)
        if idx is not None:
            cell = f"{APP_TAB}!F{idx+2}"
            sheets.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,range=cell,valueInputOption="RAW",
                body={"values":[[c]]}
            ).execute()
        logger.info(f"Processed msg {d['id']} -> {c}")

if __name__=="__main__":
    asyncio.run(process())
