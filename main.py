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
import uuid
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


def send_email_alert(text: str, reservation_id: int, subject: str):
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
        message['Subject'] = f'{subject} #{reservation_id}'
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

PAYPAL_CLIENT_ID = os.getenv('PAYPAL_CLIENT_ID', '')
PAYPAL_CLIENT_SECRET = os.getenv('PAYPAL_CLIENT_SECRET', '')
PAYPAL_ENV = os.getenv('PAYPAL_ENV', 'sandbox').lower()
PAYPAL_API_BASE = 'https://api-m.paypal.com' if PAYPAL_ENV == 'live' else 'https://api-m.sandbox.paypal.com'
PAYPAL_DEPOSIT_AMOUNT = '50.00'
PAYPAL_CURRENCY = 'USD'

def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, company TEXT, job_title TEXT,
        visit_date TEXT, party_size TEXT, vibe TEXT, budget TEXT, guide_type TEXT,
        hotel TEXT, phone TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(reservations)')}
    for name, definition in {
        'paypal_order_id': 'TEXT', 'paypal_capture_id': 'TEXT',
        'deposit_amount': 'TEXT', 'deposit_currency': 'TEXT', 'payment_status': 'TEXT'
    }.items():
        if name not in columns:
            db.execute(f'ALTER TABLE reservations ADD COLUMN {name} {definition}')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_paypal_order ON reservations(paypal_order_id)')
    db.execute('''CREATE TABLE IF NOT EXISTS pending_paypal_orders (
        order_id TEXT PRIMARY KEY, reservation_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
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
    partySize: str = Field(pattern=r'^(?:[1-4]|5\+)$')
    budget: Literal['600000 KRW per guest','800000 KRW per guest','1200000 KRW per guest']
    hotel: str = Field(min_length=1, max_length=160)
    phone: str = Field(min_length=3, max_length=40)

def paypal_access_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(503, 'PayPal checkout is not configured yet.')
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f'{PAYPAL_API_BASE}/v1/oauth2/token',
                auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                headers={'Accept': 'application/json'},
                data={'grant_type': 'client_credentials'},
            )
            response.raise_for_status()
            return response.json()['access_token']
    except Exception as exc:
        logger.warning('PayPal authentication failed (%s).', type(exc).__name__)
        raise HTTPException(502, 'Unable to connect to PayPal.') from exc


def paypal_headers(request_id: str | None = None) -> dict[str, str]:
    headers = {
        'Authorization': f'Bearer {paypal_access_token()}',
        'Content-Type': 'application/json',
    }
    if request_id:
        headers['PayPal-Request-Id'] = request_id
    return headers


def reservation_whatsapp(data: ReservationRequest, reservation_id: int, paid: bool = True) -> str | None:
    concierge_whatsapp = os.getenv('BUSAN_WHATSAPP_NUMBER', '').lstrip('+')
    if not re.fullmatch(r'[1-9][0-9]{7,14}', concierge_whatsapp):
        return None
    if paid:
        message = f'Hello, my US$50 deposit is paid. Confirmed reservation #{reservation_id}. Name: {data.name}, Date: {data.visitDate}.'
    else:
        message = f'Hello, I would like a free consultation before booking. WhatsApp inquiry #{reservation_id}. Name: {data.name}, Date: {data.visitDate}.'
    return f'https://wa.me/{concierge_whatsapp}?text={quote(message)}'


def send_paid_reservation_alert(data: ReservationRequest, reservation_id: int, capture_id: str):
    text = (
        f'[결제 완료 · 예약 확정] VIP 예약 #{reservation_id}\n'
        f'이름: {data.name}\n'
        f'날짜/인원: {data.visitDate} / {data.partySize}명\n'
        f'코스: {data.budget}\n'
        f'요청 유형: US$50 결제 / 예약 진행\n'
        f'통역: 전문 영어 통역사 배정 (비용 별도)\n'
        f'호텔: {data.hotel}\n'
        f'연락처: {data.phone}\n'
        f'상태: 예약 확정 · 장소 및 통역 배정 시작\n'
        f'후속 조치: 예약 진행 및 WhatsApp 응대\n'
        f'PayPal: {capture_id}'
    )
    send_telegram_alert(text)
    send_email_alert(text, reservation_id, '[결제 완료] VIP 예약 확정')


@app.get('/api/paypal/config')
def paypal_config():
    return {
        'enabled': bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET),
        'client_id': PAYPAL_CLIENT_ID if PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET else '',
        'environment': PAYPAL_ENV,
        'amount': PAYPAL_DEPOSIT_AMOUNT,
        'currency': PAYPAL_CURRENCY,
    }


@app.post('/api/paypal/orders')
def create_paypal_order(data: ReservationRequest):
    if not data.name.strip() or not data.hotel.strip() or not data.phone.strip():
        raise HTTPException(422, 'Name, hotel, and phone number are required.')
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f'{PAYPAL_API_BASE}/v2/checkout/orders',
                headers=paypal_headers(str(uuid.uuid4())),
                json={
                    'intent': 'CAPTURE',
                    'purchase_units': [{
                        'description': 'Midnight Sunrise Busan reservation deposit',
                        'amount': {'currency_code': PAYPAL_CURRENCY, 'value': PAYPAL_DEPOSIT_AMOUNT},
                    }],
                },
            )
            response.raise_for_status()
            order_id = response.json()['id']
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning('PayPal order creation failed (%s).', type(exc).__name__)
        raise HTTPException(502, 'Unable to create the PayPal order.') from exc
    with closing(connect_db()) as db, db:
        db.execute(
            'INSERT OR REPLACE INTO pending_paypal_orders (order_id, reservation_json) VALUES (?, ?)',
            (order_id, data.model_dump_json()),
        )
    return {'order_id': order_id}


@app.post('/api/paypal/orders/{order_id}/capture')
def capture_paypal_order(order_id: str):
    if not re.fullmatch(r'[A-Z0-9]{8,32}', order_id):
        raise HTTPException(422, 'Invalid PayPal order ID.')
    with closing(connect_db()) as db:
        existing = db.execute('SELECT id, paypal_capture_id FROM reservations WHERE paypal_order_id = ?', (order_id,)).fetchone()
        if existing:
            return {'status': 'success', 'reservation_id': existing['id'], 'capture_id': existing['paypal_capture_id'], 'whatsapp_url': None}
        pending = db.execute('SELECT reservation_json FROM pending_paypal_orders WHERE order_id = ?', (order_id,)).fetchone()
    if not pending:
        raise HTTPException(404, 'Reservation details for this PayPal order were not found.')
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f'{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture',
                headers=paypal_headers(f'capture-{order_id}'),
                json={},
            )
            response.raise_for_status()
            payment = response.json()
        capture = payment['purchase_units'][0]['payments']['captures'][0]
        amount = capture['amount']
        if payment.get('status') != 'COMPLETED' or capture.get('status') != 'COMPLETED':
            raise ValueError('PayPal payment is not completed')
        if amount.get('currency_code') != PAYPAL_CURRENCY or amount.get('value') != PAYPAL_DEPOSIT_AMOUNT:
            raise ValueError('PayPal payment amount does not match')
        capture_id = capture['id']
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning('PayPal capture failed for order %s (%s).', order_id, type(exc).__name__)
        raise HTTPException(502, 'PayPal could not confirm the payment.') from exc

    data = ReservationRequest.model_validate_json(pending['reservation_json'])
    with closing(connect_db()) as db, db:
        cursor = db.execute('''INSERT INTO reservations
            (name, company, job_title, visit_date, party_size, vibe, budget, guide_type, hotel, phone,
             paypal_order_id, paypal_capture_id, deposit_amount, deposit_currency, payment_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            data.name.strip(), data.company, data.jobTitle, str(data.visitDate), data.partySize,
            'Private VIP', data.budget, 'Fluent English Interpreter', data.hotel.strip(), data.phone.strip(),
            order_id, capture_id, PAYPAL_DEPOSIT_AMOUNT, PAYPAL_CURRENCY, 'paid',
        ))
        reservation_id = cursor.lastrowid
        db.execute('DELETE FROM pending_paypal_orders WHERE order_id = ?', (order_id,))
    send_paid_reservation_alert(data, reservation_id, capture_id)
    return {
        'status': 'success', 'reservation_id': reservation_id, 'capture_id': capture_id,
        'whatsapp_url': reservation_whatsapp(data, reservation_id),
    }


@app.post('/api/reservation')
def create_unpaid_reservation(data: ReservationRequest):
    if not data.name.strip() or not data.hotel.strip() or not data.phone.strip():
        raise HTTPException(422, 'Name, hotel, and phone number are required.')
    with closing(connect_db()) as db, db:
        cursor = db.execute('''INSERT INTO reservations
            (name, company, job_title, visit_date, party_size, vibe, budget, guide_type, hotel, phone,
             payment_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            data.name.strip(), data.company, data.jobTitle, str(data.visitDate), data.partySize,
            'Private VIP', data.budget, 'Fluent English Interpreter', data.hotel.strip(), data.phone.strip(), 'unpaid',
        ))
        reservation_id = cursor.lastrowid
    text = (
        f'[무료 상담 요청] WhatsApp 문의 #{reservation_id}\n'
        f'이름: {data.name}\n'
        f'날짜/인원: {data.visitDate} / {data.partySize}명\n'
        f'코스: {data.budget}\n'
        f'요청 유형: WhatsApp 무료 상담\n'
        f'통역: 비용 별도 · 결제 후 배정\n'
        f'호텔: {data.hotel}\n'
        f'연락처: {data.phone}\n'
        f'상태: 결제 전 · 예약 미확정\n'
        f'후속 조치: WhatsApp으로 상담 진행'
    )
    send_telegram_alert(text)
    send_email_alert(text, reservation_id, '[무료 상담] WhatsApp 문의')
    return {
        'status': 'success', 'reservation_id': reservation_id, 'payment_status': 'unpaid',
        'whatsapp_url': reservation_whatsapp(data, reservation_id, paid=False),
    }

@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html')

@app.get('/{asset}')
def static_asset(asset: str):
    allowed = {'index.html', 'course-results.js', 'courses.css', 'api-config.js', 'booking-api.js', 'google9b519aff934fd839.html', 'robots.txt', 'sitemap.xml'}
    if asset not in allowed and not re.fullmatch(r'(?:main|mobile_main|image1 \(\d+\))\.png', asset):
        raise HTTPException(404)
    path = ROOT / asset
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.getenv('BUSAN_HOST', '127.0.0.1'), port=int(os.getenv('PORT', os.getenv('BUSAN_PHONE_PORT', '8001'))))

