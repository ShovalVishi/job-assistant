#!/usr/bin/env python3
import os
import json
import base64 as b64lib
import logging
import asyncio
from email.mime.text import MIMEText

import openai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ------------------ CONFIGURATION ------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger()

# OpenAI API Key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Google Service Account with domain-wide delegation
b64 = os.getenv("SERVICE_ACCOUNT_JSON_B64", "")
if not b64:
    logger.error("SERVICE_ACCOUNT_JSON_B64 not set!")
service_info = json.loads(b64lib.b64decode(b64))
DELEGATE_EMAIL = os.getenv("GMAIL_DELEGATE_EMAIL")  # the actual user email to impersonate

# Scopes needed
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/spreadsheets"
]

# Create credentials with delegation
creds = service_account.Credentials.from_service_account_info(
    service_info,
    scopes=SCOPES,
    subject=DELEGATE_EMAIL
)

# Build Gmail and Sheets services using keyword credentials
gmail = build("gmail", "v1", credentials=creds)
sheets = build("sheets", "v4", credentials=creds)

SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
APPLICATIONS_TAB = os.getenv("GOOGLE_SHEET_APP_TAB", "Applications")

async def classify_response(body: str) -> str:
    prompt = (
        "Please classify the following email response as 'positive' (interested/follow-up) "
        "or 'negative' (rejection), and reply with only the word positive or negative:\n\n"
        + body
    )
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}]
    )
    return resp.choices[0].message.content.strip().lower()

async def draft_reply(original_msg: dict, body: str) -> None:
    prompt = (
        "Craft a concise, professional email reply to the following positive response:\n\n"
        + body
    )
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}]
    )
    reply_text = resp.choices[0].message.content.strip()

    mime = MIMEText(reply_text)
    mime["To"] = original_msg["from"]
    mime["From"] = DELEGATE_EMAIL
    mime["Subject"] = "Re: " + original_msg.get("subject", "")

    raw = b64lib.urlsafe_b64encode(mime.as_bytes()).decode()
    draft_body = {"message": {"raw": raw}}
    try:
        gmail.users().drafts().create(userId="me", body=draft_body).execute()
        logger.info("Draft created for positive response")
    except HttpError as e:
        logger.error(f"Gmail draft error: {e.resp.status} {e.content}")

def list_responses() -> list:
    try:
        result = gmail.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            q="subject:(Re:) is:unread"
        ).execute()
    except HttpError as e:
        logger.error(f"Gmail list error: {e.resp.status} {e.content}")
        return []
    return result.get("messages", [])

def get_message(msg_id: str) -> dict:
    msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    body = ""
    parts = msg["payload"].get("parts", [])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part["body"]["data"]
            body = b64lib.urlsafe_b64decode(data).decode()
            break
    return {
        "id": msg_id,
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "body": body
    }

async def process_responses():
    msgs = list_responses()
    if not msgs:
        logger.info("No new responses")
        return

    # Fetch existing Application links to find the correct row
    sheet_data = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{APPLICATIONS_TAB}!C:C"
    ).execute().get("values", [])

    for m in msgs:
        detail = get_message(m["id"])
        classification = await classify_response(detail["body"])

        # Find row index by matching link in subject
        row_index = next(
            (i for i, row in enumerate(sheet_data) if row and row[0] in detail["subject"]),
            None
        )
        if row_index is None:
            logger.warning("No matching application found for response")
            continue

        # Update the response column (F) for that row
        cell = f"{APPLICATIONS_TAB}!F{row_index+2}"
        sheets.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[classification]]}
        ).execute()
        logger.info(f"Updated row {row_index+2} with status '{classification}'")

        if classification == "positive":
            await draft_reply(detail, detail["body"])

if __name__ == "__main__":
    asyncio.run(process_responses())
