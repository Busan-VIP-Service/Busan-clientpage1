from datetime import date
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib.parse import quote
import os
import re
import sqlite3
import smtplib
import ssl
import logging
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('BUSAN_DB_PATH', str(ROOT.parent / 'reservations.db')))
app = FastAPI(title='Busan Private Concierge · Direct Booking')
logger = logging.getLogger(__name__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip().rstrip('/') for origin in os.getenv(
        'BUSAN_ALLOWED_ORIGINS', 'https://busan-vip-service.github.io'
    ).split(',') if origin.strip()],
    allow_methods=['POST', 'GET'],
    allow_headers=['Content-Type', 'x-busan-request'],
)

@app.get('/api/health')
def health():
    return {'status': 'ok'}


# Enable email notifications after configuring a sending SMTP account.
EMAIL_ALERT_ENABLED = os.getenv('EMAIL_ALERT_ENABLED', 'false').lower() in {'1', 'true', 'yes'}
EMAIL_ALERT_TO = os.getenv('EMAIL_ALERT_TO', 'jwh2394@naver.com')


def send_email_alert(text: str, reservation_id: int):
    if not EMAIL_ALERT_ENABLED:
        return
    try:
        host = os.getenv('SMTP_HOST', '')
        username = os.getenv('SMTP_USERNAME', '')
        password = os.getenv('SMTP_PASSWORD', '')
        sender = os.getenv('SMTP_FROM', username)
        if not all((host, username, password, sender, EMAIL_ALERT_TO)):
            logger.warning('Email alert skipped: SMTP configuration is incomplete.')
            return
        message = EmailMessage()
        message['Subject'] = f'Busan VIP booking request #{reservation_id}'
        message['From'] = sender
        message['To'] = EMAIL_ALERT_TO
        message.set_content(text)
        security = os.getenv('SMTP_SECURITY', 'ssl').lower()
        context = ssl.create_default_context()
        if security == 'ssl':
            server = smtplib.SMTP_SSL(host, int(os.getenv('SMTP_PORT', '465')), timeout=10, context=context)
        elif security == 'starttls':
            server = smtplib.SMTP(host, int(os.getenv('SMTP_PORT', '587')), timeout=10)
        else:
            raise ValueError('SMTP_SECURITY must be ssl or starttls')
        with server:
            if security == 'starttls':
                server.starttls(context=context)
            server.login(username, password)
            server.send_message(message)
    except Exception as exc:
        # Keep the saved reservation even when the notification service fails.
        logger.warning('Email alert failed (%s), reservation #%s remains saved.', type(exc).__name__, reservation_id)

# 텔레그램 봇 설정 (환경 변수 또는 여기에 직접 토큰과 챗ID를 박아도 됩니다)
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, job_title TEXT,
        visit_date TEXT, party_size TEXT, vibe TEXT, budget TEXT, guide_type TEXT,
        hotel TEXT, phone TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    db.commit()
    return db

def send_telegram_alert(text: str):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == '여기에_텔레그램_봇_토큰_입력':
        logger.warning('Telegram alert skipped: bot token is missing.')
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=10) as client:
            response = client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
            result = response.json()
            if not response.is_success or not result.get('ok'):
                logger.warning('Telegram alert rejected: %s', result.get('description', 'Unknown API error'))
    except Exception as exc:
        logger.warning('Telegram alert failed (%s).', type(exc).__name__)

class ReservationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    company: str = Field(default='', max_length=120)
    jobTitle: str = Field(default='', max_length=120)
    visitDate: date
    partySize: str = Field(pattern=r'^(?:[1-9][0-9]?|100)$')
    vibe: Literal['Casual Bar','Dynamic Night','Ultimate VIP']
    budget: Literal['500 - 1000','1000 - 2000','2000 - 3000','No limit']
    guideType: Literal['Professional Interpreter','Basic Guide']
    hotel: str = Field(min_length=1, max_length=160)
    phone: str = Field(min_length=3, max_length=40)

@app.post('/api/reservation')
def create_reservation(data: ReservationRequest):
    if not data.name.strip() or not data.hotel.strip() or not data.phone.strip():
        raise HTTPException(422, 'Name, hotel, and phone number are required.')
    
    with closing(connect_db()) as db, db:
        cursor = db.execute('''INSERT INTO reservations 
            (name, company, job_title, visit_date, party_size, vibe, budget, guide_type, hotel, phone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            data.name.strip(), data.company, data.jobTitle, str(data.visitDate),
            data.partySize, data.vibe, data.budget, data.guideType, data.hotel.strip(), data.phone.strip()
        ))
        reservation_id = cursor.lastrowid

    # 텔레그램 메시지 포맷팅
    tg_text = (
        f"🚨 *새로운 부산 VIP 예약 신청!*\n\n"
        f"🆔 *No.* {reservation_id}\n"
        f"👤 *이름:* {data.name} ({data.company or 'Individual'} / {data.jobTitle or '-'})\n"
        f"📅 *방문일:* {data.visitDate}\n"
        f"👥 *인원:* {data.partySize}명\n"
        f"✨ *무드:* {data.vibe}\n"
        f"💰 *예산:* ${data.budget}\n"
        f"🗣️ *통역사:* {data.guideType}\n"
        f"🏨 *호텔:* {data.hotel}\n"
        f"📱 *연락처(WhatsApp):* `{data.phone}`"
    )
    send_telegram_alert(tg_text)
    send_email_alert(tg_text, reservation_id)

    # 사장님 왓츠앱 다이렉트 링크 생성
    concierge_whatsapp = os.getenv('BUSAN_WHATSAPP_NUMBER', '').lstrip('+')
    whatsapp_link = None
    if re.fullmatch(r'[1-9][0-9]{7,14}', concierge_whatsapp):
        message = f"Hello, I just requested a Busan curation. Request #{reservation_id}. Name: {data.name}, Date: {data.visitDate}."
        whatsapp_link = f"https://wa.me/{concierge_whatsapp}?text={quote(message)}"

    return {
        'status': 'success',
        'reservation_id': reservation_id,
        'whatsapp_url': whatsapp_link,
        'message': 'Your request has been successfully submitted.'
    }

@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html')


@app.get('/{asset}')
def static_asset(asset: str):
    allowed = {
        'index.html',
        'course-results.js',
        'courses.css',
        'api-config.js',
        'booking-api.js',
        'google9b519aff934fd839.html',
    }
    if asset not in allowed and not re.fullmatch(r'(?:main|mobile_main|image1 \(\d+\))\.png', asset):
        raise HTTPException(404)
    path = ROOT / asset
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv('BUSAN_HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', os.getenv('BUSAN_PHONE_PORT', '8001'))),
    )
