# Job Assistant

This project automates your job search:
- Twice-daily scrape and filter of relevant roles
- Tailored résumé & cover letter generation via OpenAI
- Notifications & approvals via Telegram
- Submission by email
- Logging to Google Sheets

## Files

- `job_assistant.py`: Main Python script
- `requirements.txt`: Python dependencies
- `.github/workflows/scheduled_job.yml`: GitHub Actions workflow

## Setup & Usage

1. **Clone the repo**  
   ```bash
   git clone https://github.com/<your-username>/job-assistant.git
   cd job-assistant
   ```

2. **Add GitHub Secrets** in Settings → Secrets → Actions:  
   - `OPENAI_API_KEY`
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GMAIL_USERNAME`
   - `GMAIL_PASSWORD`
   - `GOOGLE_SHEETS_ID`
   - `SERVICE_ACCOUNT_JSON` (entire JSON key)

3. **Push files** and enable Actions.  
4. **Run workflow manually** to test; then it will auto-run twice daily.

See main instructions in the repo.