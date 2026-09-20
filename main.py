from datetime import date, datetime, timezone, timedelta
from contextlib import closing
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib.parse import quote
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
import logging
import time
import uuid
import httpx
import psycopg2
import psycopg2.extras
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.getenv('DATABASE_URL', '')

app = FastAPI(title='Busan Private Concierge · Direct Booking')
logger = logging.getLogger(__name__)
ALLOWED_ORIGINS = {
    'https://midnightsunrisebusan.com',
    'https://www.midnightsunrisebusan.com',
    'https://busan-vip-service.github.io',
} | {origin.strip().rstrip('/') for origin in os.getenv('BUSAN_ALLOWED_ORIGINS', '').split(',') if origin.strip()}
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_methods=['POST', 'GET'],
    allow_headers=['Content-Type', 'x-busan-request'],
)

@app.get('/api/health')
def health():
    return {'status': 'ok'}
    
@app.get('/guide.html')
def get_guide():
    return FileResponse('guide.html')

@app.get('/guide')
def get_guide_clean():
    return FileResponse('guide.html')

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
        logger.warning('Email alert failed (%s), reservation #%s remains saved.', type(exc).__name__, reservation_id)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

PAYPAL_CLIENT_ID = os.getenv('PAYPAL_CLIENT_ID', '')
PAYPAL_CLIENT_SECRET = os.getenv('PAYPAL_CLIENT_SECRET', '')
PAYPAL_ENV = os.getenv('PAYPAL_ENV', 'sandbox').lower()
PAYPAL_API_BASE = 'https://api-m.paypal.com' if PAYPAL_ENV == 'live' else 'https://api-m.sandbox.paypal.com'
PAYPAL_DEPOSIT_AMOUNT = '50.00'
PAYPAL_CURRENCY = 'USD'
PAYPAL_ADMIN_USE_CHECKOUT = os.getenv('PAYPAL_ADMIN_USE_CHECKOUT', 'true').lower() in {'1', 'true', 'yes'}
ADMIN_PASSWORD = os.getenv('BUSAN_ADMIN_PASSWORD', '')
ADMIN_SESSION_SECRET = os.getenv('BUSAN_ADMIN_SESSION_SECRET', '') or hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
ADMIN_COOKIE = 'busan_admin_session'
ADMIN_SESSION_SECONDS = 30 * 24 * 60 * 60
ADMIN_LOGIN_ATTEMPTS: dict[str, list[float]] = {}

def connect_db():
    if not DATABASE_URL:
        raise HTTPException(500, 'DATABASE_URL environment variable is not configured.')
    db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    with db.cursor() as cursor:
        cursor.execute('''CREATE TABLE IF NOT EXISTS reservations (
            id SERIAL PRIMARY KEY, name TEXT, company TEXT, job_title TEXT,
            visit_date TEXT, party_size TEXT, vibe TEXT, budget TEXT, guide_type TEXT,
            hotel TEXT, phone TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'reservations'")
        columns = {row['column_name'] for row in cursor.fetchall()}
        for name, definition in {
            'paypal_order_id': 'TEXT', 'paypal_capture_id': 'TEXT',
            'deposit_amount': 'TEXT', 'deposit_currency': 'TEXT', 'payment_status': 'TEXT'
        }.items():
            if name not in columns:
                cursor.execute(f'ALTER TABLE reservations ADD COLUMN {name} {definition}')
                
        cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_paypal_order ON reservations(paypal_order_id)')
        cursor.execute('ALTER TABLE reservations ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMPTZ')
        cursor.execute('ALTER TABLE reservations ADD COLUMN IF NOT EXISTS verification_token_hash TEXT')
        cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_verification_token ON reservations(verification_token_hash)')
        cursor.execute('''CREATE TABLE IF NOT EXISTS phone_verifications (
            token_hash TEXT PRIMARY KEY, phone TEXT NOT NULL, ip_hash TEXT NOT NULL,
            provider_sid TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            verified_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ)''')
        cursor.execute('ALTER TABLE phone_verifications ADD COLUMN IF NOT EXISTS code_hash TEXT')
        cursor.execute('CREATE INDEX IF NOT EXISTS phone_verifications_phone_time ON phone_verifications(phone, created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS phone_verifications_ip_time ON phone_verifications(ip_hash, created_at)')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS pending_paypal_orders (
            order_id TEXT PRIMARY KEY, reservation_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            
        cursor.execute('''CREATE TABLE IF NOT EXISTS admin_invoices (
            invoice_id TEXT PRIMARY KEY, reservation_id INTEGER, customer_name TEXT,
            customer_email TEXT, course_name TEXT, total_amount TEXT, currency TEXT,
            payer_url TEXT, status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    db.commit()
    return db


def analytics_db():
    db = connect_db()
    with db.cursor() as cursor:
        cursor.execute('''CREATE TABLE IF NOT EXISTS page_views (
            event_id TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL)''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_page_views_day ON page_views(day)')
    db.commit()
    return db


class PageViewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    source: Literal['qr', 'direct', 'search', 'referral']


@app.post('/api/analytics/page-view', status_code=204)
def record_page_view(data: PageViewRequest, request: Request):
    if request.headers.get('x-busan-request') != '1':
        raise HTTPException(400, 'Browser request required.')
    if re.search(r'bot|crawler|spider|headless', request.headers.get('user-agent', ''), re.I):
        return Response(status_code=204)
    try:
        require_admin(request)
        return Response(status_code=204)
    except HTTPException:
        pass
    day = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    with closing(analytics_db()) as db, db, db.cursor() as cursor:
        cursor.execute('INSERT INTO page_views (event_id, day, source) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING',
                       (data.event_id, day, data.source))
    return Response(status_code=204)


@app.get('/api/admin/analytics')
def admin_analytics(request: Request, response: Response):
    require_admin(request)
    response.headers['Cache-Control'] = 'no-store'
    today = datetime.now(timezone(timedelta(hours=9))).date()
    start = (today - timedelta(days=29)).isoformat()
    with closing(analytics_db()) as db, db.cursor() as cursor:
        cursor.execute('''SELECT COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN day = %s THEN 1 ELSE 0 END), 0) AS today,
            COALESCE(SUM(CASE WHEN day >= %s THEN 1 ELSE 0 END), 0) AS week, MIN(day) AS started
            FROM page_views''', (today.isoformat(), (today - timedelta(days=6)).isoformat()))
        totals = dict(cursor.fetchone())
        
        cursor.execute('SELECT day, COUNT(*) AS views FROM page_views WHERE day >= %s GROUP BY day', (start,))
        counts = {r['day']: r['views'] for r in cursor.fetchall()}
        
        cursor.execute('SELECT source, COUNT(*) AS views FROM page_views WHERE day >= %s GROUP BY source', (start,))
        sources = {r['source']: r['views'] for r in cursor.fetchall()}
        
    return {**totals, 'days': [{'day': (today - timedelta(days=i)).isoformat(),
            'views': counts.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(30)],
            'sources': sources}


def issue_admin_session() -> str:
    timestamp = str(int(time.time()))
    signature = hmac.new(ADMIN_SESSION_SECRET.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
    return f'{timestamp}.{signature}'


def require_admin(request: Request):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, 'Admin access is not configured yet.')
    token = request.cookies.get(ADMIN_COOKIE, '')
    try:
        timestamp, signature = token.split('.', 1)
        valid_age = 0 <= int(time.time()) - int(timestamp) <= ADMIN_SESSION_SECONDS
        expected = hmac.new(ADMIN_SESSION_SECRET.encode(), timestamp.encode(), hashlib.sha256).hexdigest()
    except (ValueError, TypeError):
        raise HTTPException(401, 'Admin login required.')
    if not valid_age or not secrets.compare_digest(signature, expected):
        raise HTTPException(401, 'Admin login required.')

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
    verificationToken: str = Field(min_length=40, max_length=100)


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=200)


PHONE_RE = re.compile(r'^\+[1-9][0-9]{7,14}$')

class PhoneStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phone: str
    countryCode: str = Field(pattern=r'^[1-9][0-9]{0,3}$')

class PhoneCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str
    code: str

def phone_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def require_phone_request(request: Request):
    if request.headers.get('x-busan-request') != '1':
        raise HTTPException(403, 'Invalid request.')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') not in ALLOWED_ORIGINS:
        raise HTTPException(403, 'Invalid origin.')

def otp_hash(token: str, code: str) -> str:
    pepper = os.getenv('SOLAPI_OTP_SECRET', '')
    if len(pepper) < 32:
        raise HTTPException(503, 'SMS verification is not configured yet.')
    return hmac.new(pepper.encode(), f'{token}:{code}'.encode(), hashlib.sha256).hexdigest()


def send_otp_sms(phone: str, country_code: str, code: str):
    key = os.getenv('SOLAPI_API_KEY', '')
    secret = os.getenv('SOLAPI_API_SECRET', '')
    sender = os.getenv('SOLAPI_SENDER', '')
    if not all((key, secret, sender)):
        raise HTTPException(503, 'SMS verification is not configured yet.')
    prefix = '+' + country_code
    if not phone.startswith(prefix) or len(phone) <= len(prefix) + 3:
        raise HTTPException(422, 'Phone number and country code do not match.')
    recipient = phone[len(prefix):]
    if country_code == '82':
        recipient = '0' + recipient
    try:
        from solapi import SolapiMessageService
        from solapi.model import RequestMessage
        service = SolapiMessageService(api_key=key, api_secret=secret)
        response = service.send(RequestMessage(
            from_=sender, to=recipient, country=country_code,
            text=f'Midnight Sunrise Busan Verification Code: {code}'
        ))
        if response.group_info.count.registered_success != 1:
            raise RuntimeError('SMS was not accepted for sending')
    except Exception as exc:
        logger.warning('SMS provider failed (%s).', type(exc).__name__)
        raise HTTPException(503, 'SMS verification is temporarily unavailable.') from exc

@app.post('/api/phone/start')
def start_phone_verification(data: PhoneStart, request: Request):
    require_phone_request(request)
    phone = data.phone.strip()
    if not PHONE_RE.fullmatch(phone):
        raise HTTPException(422, 'Enter a valid international phone number.')
    if not phone.startswith('+' + data.countryCode) or len(phone) <= len(data.countryCode) + 4:
        raise HTTPException(422, 'Phone number and country code do not match.')
    if not all(os.getenv(name) for name in ('SOLAPI_API_KEY', 'SOLAPI_API_SECRET', 'SOLAPI_SENDER')):
        raise HTTPException(503, 'SMS verification is not configured yet.')
    code = f'{secrets.randbelow(1000000):06d}'
    code_digest = otp_hash(token := secrets.token_urlsafe(32), code)
    ip_hash = phone_hash(request.client.host if request.client else 'unknown')
    with closing(connect_db()) as db, db.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (phone,))
        cursor.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (ip_hash,))
        cursor.execute("DELETE FROM phone_verifications WHERE created_at < NOW() - INTERVAL '2 days'")
        cursor.execute("SELECT COUNT(*) AS n FROM phone_verifications WHERE phone=%s AND created_at > NOW()-INTERVAL '1 hour'", (phone,))
        phone_hour = cursor.fetchone()['n']
        cursor.execute("SELECT COUNT(*) AS n FROM phone_verifications WHERE ip_hash=%s AND created_at > NOW()-INTERVAL '1 hour'", (ip_hash,))
        ip_hour = cursor.fetchone()['n']
        cursor.execute("SELECT created_at FROM phone_verifications WHERE phone=%s ORDER BY created_at DESC LIMIT 1", (phone,))
        last = cursor.fetchone()
        if phone_hour >= 5 or ip_hour >= 15 or (last and (datetime.now(timezone.utc)-last['created_at']).total_seconds() < 60):
            raise HTTPException(429, 'Please wait before requesting another code.', headers={'Retry-After': '60'})
        # Record the attempt before contacting the provider so failures cannot bypass the budget.
        cursor.execute("INSERT INTO phone_verifications(token_hash,phone,ip_hash,code_hash,expires_at) VALUES (%s,%s,%s,%s,NOW()+INTERVAL '10 minutes')", (phone_hash(token), phone, ip_hash, code_digest))
        db.commit()
    send_otp_sms(phone, data.countryCode, code)
    return {'token': token, 'expires_in': 600, 'retry_after': 60}

@app.post('/api/phone/check')
def check_phone_verification(data: PhoneCheck, request: Request):
    require_phone_request(request)
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', data.token) or not re.fullmatch(r'[0-9]{6}', data.code):
        raise HTTPException(422, 'Invalid verification code.')
    with closing(connect_db()) as db, db, db.cursor() as cursor:
        cursor.execute('SELECT * FROM phone_verifications WHERE token_hash=%s FOR UPDATE', (phone_hash(data.token),))
        row = cursor.fetchone()
        if not row or row['expires_at'] <= datetime.now(timezone.utc) or row['consumed_at']:
            raise HTTPException(410, 'Verification expired. Request a new code.')
        if row['attempts'] >= 5:
            raise HTTPException(429, 'Too many code attempts.')
        if not row['code_hash']:
            raise HTTPException(503, 'SMS verification is temporarily unavailable.')
        cursor.execute('UPDATE phone_verifications SET attempts=attempts+1 WHERE token_hash=%s', (phone_hash(data.token),))
        if not hmac.compare_digest(row['code_hash'], otp_hash(data.token, data.code)):
            db.commit()
            raise HTTPException(422, 'The code is incorrect or expired.')
        cursor.execute('UPDATE phone_verifications SET verified_at=NOW() WHERE token_hash=%s AND expires_at>NOW() AND consumed_at IS NULL RETURNING phone', (phone_hash(data.token),))
        verified = cursor.fetchone()
        if not verified:
            raise HTTPException(410, 'Verification expired. Request a new code.')
        db.commit()
    return {'verified': True, 'phone': verified['phone']}


class AdminInvoiceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reservationId: int | None = Field(default=None, ge=1)
    customerName: str = Field(min_length=1, max_length=80)
    customerEmail: str = Field(default='', max_length=254)
    courseName: str = Field(min_length=1, max_length=100)
    courseUsd: Decimal = Field(default=Decimal('0'), ge=0, le=100000)
    guestCount: int = Field(default=1, ge=1, le=30)
    interpreterUsd: Decimal = Field(default=Decimal('0'), ge=0, le=100000)
    additionalUsd: Decimal = Field(default=Decimal('0'), ge=0, le=100000)
    discountPercent: Literal[0, 5, 10, 15, 20] = 0
    note: str = Field(default='', max_length=500)

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
        raise HTTPException(502, 'PayPal is temporarily unavailable. Please continue on WhatsApp.') from exc


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
        message = f'Hello, I submitted free reservation #{reservation_id}. Name: {data.name}, Date: {data.visitDate}. Please confirm availability and details.'
    return f'https://wa.me/{concierge_whatsapp}?text={quote(message)}'


@app.get('/api/whatsapp')
def open_whatsapp_chat():
    concierge_whatsapp = os.getenv('BUSAN_WHATSAPP_NUMBER', '').lstrip('+')
    if not re.fullmatch(r'[1-9][0-9]{7,14}', concierge_whatsapp):
        raise HTTPException(503, 'WhatsApp consultation is not configured yet.')
    message = quote('Hello, I would like a free consultation about a Midnight Sunrise Busan reservation.')
    return RedirectResponse(f'https://wa.me/{concierge_whatsapp}?text={message}', status_code=302)


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
    with closing(connect_db()) as db, db, db.cursor() as cursor:
        cursor.execute('''UPDATE phone_verifications SET consumed_at=NOW()
            WHERE token_hash=%s AND phone=%s AND verified_at IS NOT NULL
            AND expires_at>NOW() AND consumed_at IS NULL RETURNING verified_at''',
            (phone_hash(data.verificationToken), data.phone.strip()))
        if not cursor.fetchone():
            raise HTTPException(403, 'Verify this phone number before booking.')
        cursor.execute(
            'INSERT INTO pending_paypal_orders (order_id, reservation_json) VALUES (%s, %s) ON CONFLICT (order_id) DO UPDATE SET reservation_json = EXCLUDED.reservation_json',
            (order_id, data.model_dump_json()),
        )
        db.commit()
    return {'order_id': order_id}


@app.post('/api/paypal/orders/{order_id}/capture')
def capture_paypal_order(order_id: str):
    if not re.fullmatch(r'[A-Z0-9]{8,32}', order_id):
        raise HTTPException(422, 'Invalid PayPal order ID.')
    with closing(connect_db()) as db, db.cursor() as cursor:
        cursor.execute('SELECT id, paypal_capture_id FROM reservations WHERE paypal_order_id = %s', (order_id,))
        existing = cursor.fetchone()
        if existing:
            return {'status': 'success', 'reservation_id': existing['id'], 'capture_id': existing['paypal_capture_id'], 'whatsapp_url': None}
        cursor.execute('SELECT reservation_json FROM pending_paypal_orders WHERE order_id = %s', (order_id,))
        pending = cursor.fetchone()
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
    with closing(connect_db()) as db, db, db.cursor() as cursor:
        cursor.execute('SELECT verified_at FROM phone_verifications WHERE token_hash=%s AND phone=%s AND consumed_at IS NOT NULL',
                       (phone_hash(data.verificationToken), data.phone.strip()))
        verified = cursor.fetchone()
        if not verified:
            raise HTTPException(403, 'Phone verification is missing for this order.')
        cursor.execute('''INSERT INTO reservations
            (name, company, job_title, visit_date, party_size, vibe, budget, guide_type, hotel, phone,
             paypal_order_id, paypal_capture_id, deposit_amount, deposit_currency, payment_status, phone_verified_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''', (
            data.name.strip(), data.company, data.jobTitle, str(data.visitDate), data.partySize,
            'Private VIP', data.budget, 'Fluent English Interpreter', data.hotel.strip(), data.phone.strip(),
            order_id, capture_id, PAYPAL_DEPOSIT_AMOUNT, PAYPAL_CURRENCY, 'paid', verified['verified_at'],
        ))
        reservation_id = cursor.fetchone()['id']
        cursor.execute('DELETE FROM pending_paypal_orders WHERE order_id = %s', (order_id,))
        db.commit()
    send_paid_reservation_alert(data, reservation_id, capture_id)
    return {
        'status': 'success', 'reservation_id': reservation_id, 'capture_id': capture_id,
        'whatsapp_url': reservation_whatsapp(data, reservation_id),
    }


@app.post('/api/reservation')
def create_free_reservation(data: ReservationRequest, background_tasks: BackgroundTasks):
    if not data.name.strip() or not data.hotel.strip() or not data.phone.strip():
        raise HTTPException(422, 'Name, hotel, and phone number are required.')
    token_hash = phone_hash(data.verificationToken)
    with closing(connect_db()) as db, db, db.cursor() as cursor:
        cursor.execute('SELECT id FROM reservations WHERE verification_token_hash=%s AND phone=%s',
                       (token_hash, data.phone.strip()))
        existing = cursor.fetchone()
        if existing:
            return {
                'status': 'success', 'reservation_id': existing['id'], 'payment_status': 'free_reservation',
                'whatsapp_url': reservation_whatsapp(data, existing['id'], paid=False),
            }
        cursor.execute('''UPDATE phone_verifications SET consumed_at=NOW()
            WHERE token_hash=%s AND phone=%s AND verified_at IS NOT NULL
            AND expires_at>NOW() AND consumed_at IS NULL RETURNING verified_at''',
            (token_hash, data.phone.strip()))
        verified = cursor.fetchone()
        if not verified:
            cursor.execute('SELECT id FROM reservations WHERE verification_token_hash=%s AND phone=%s',
                           (token_hash, data.phone.strip()))
            existing = cursor.fetchone()
            if existing:
                return {
                    'status': 'success', 'reservation_id': existing['id'], 'payment_status': 'free_reservation',
                    'whatsapp_url': reservation_whatsapp(data, existing['id'], paid=False),
                }
            raise HTTPException(403, 'Phone verification expired. Request a new code before booking.')
        cursor.execute('''INSERT INTO reservations
            (name, company, job_title, visit_date, party_size, vibe, budget, guide_type, hotel, phone,
             payment_status, phone_verified_at, verification_token_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''', (
            data.name.strip(), data.company, data.jobTitle, str(data.visitDate), data.partySize,
            'Private VIP', data.budget, 'Fluent English Interpreter', data.hotel.strip(), data.phone.strip(), 'free_reservation', verified['verified_at'], token_hash,
        ))
        reservation_id = cursor.fetchone()['id']
        db.commit()
    text = (
        f'[무료 예약 요청] VIP 예약 #{reservation_id}\n'
        f'이름: {data.name}\n'
        f'날짜/인원: {data.visitDate} / {data.partySize}명\n'
        f'코스: {data.budget}\n'
        f'요청 유형: 무료 예약 신청\n'
        f'통역: 비용 별도 · 상담 후 배정\n'
        f'호텔: {data.hotel}\n'
        f'연락처: {data.phone}\n'
        f'상태: 무료 예약 접수 · 일정 확인 필요\n'
        f'후속 조치: WhatsApp으로 가능 여부 및 상세 내용 확인'
    )
    background_tasks.add_task(send_telegram_alert, text)
    background_tasks.add_task(send_email_alert, text, reservation_id, '[무료 예약] VIP 예약 요청')
    return {
        'status': 'success', 'reservation_id': reservation_id, 'payment_status': 'free_reservation',
        'whatsapp_url': reservation_whatsapp(data, reservation_id, paid=False),
    }


@app.post('/api/admin/login')
def admin_login(data: AdminLoginRequest, request: Request, response: Response):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, 'Admin access is not configured yet.')
    address = request.client.host if request.client else 'unknown'
    now = time.time()
    attempts = [stamp for stamp in ADMIN_LOGIN_ATTEMPTS.get(address, []) if now - stamp < 600]
    ADMIN_LOGIN_ATTEMPTS[address] = attempts
    if len(attempts) >= 5:
        raise HTTPException(429, 'Too many attempts. Please wait 10 minutes.')
    if not secrets.compare_digest(data.password, ADMIN_PASSWORD):
        attempts.append(now)
        raise HTTPException(401, 'Incorrect password.')
    ADMIN_LOGIN_ATTEMPTS.pop(address, None)
    response.set_cookie(
        ADMIN_COOKIE, issue_admin_session(), max_age=ADMIN_SESSION_SECONDS,
        httponly=True, secure=True, samesite='strict', path='/'
    )
    return {'authenticated': True}


@app.get('/api/admin/session')
def admin_session(request: Request):
    require_admin(request)
    return {'authenticated': True}


@app.post('/api/admin/logout')
def admin_logout(request: Request, response: Response):
    require_admin(request)
    response.delete_cookie(ADMIN_COOKIE, path='/')
    return {'authenticated': False}


@app.get('/api/admin/reservations')
def admin_reservations(request: Request):
    require_admin(request)
    with closing(connect_db()) as db, db.cursor() as cursor:
        cursor.execute('''SELECT id, name, visit_date, party_size, budget, hotel, phone,
            payment_status, deposit_amount, deposit_currency, created_at
            FROM reservations ORDER BY id DESC LIMIT 100''')
        rows = cursor.fetchall()
    return {'reservations': [dict(row) for row in rows]}


@app.get('/api/admin/paypal-invoicing-status')
def admin_paypal_invoicing_status(request: Request):
    require_admin(request)
    try:
        with httpx.Client(timeout=20) as client:
            response = client.get(
                f'{PAYPAL_API_BASE}/v2/invoicing/invoices',
                headers=paypal_headers(),
                params={'page': 1, 'page_size': 1, 'total_required': 'false'},
            )
        if response.is_success:
            return {'enabled': True}
        logger.warning('PayPal invoicing status failed: status=%s body=%s', response.status_code, response.text[:1600])
        return {'enabled': False, 'status': response.status_code}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning('PayPal invoicing status check failed (%s).', type(exc).__name__)
        raise HTTPException(502, 'Unable to verify PayPal Invoicing right now.') from exc


@app.post('/api/admin/invoices')
def create_admin_invoice(data: AdminInvoiceRequest, request: Request):
    require_admin(request)
    if data.customerEmail and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data.customerEmail):
        raise HTTPException(422, 'Enter a valid customer email or leave it blank.')
    course_total = data.courseUsd * data.guestCount
    subtotal = course_total + data.interpreterUsd + data.additionalUsd
    discount_amount = subtotal * Decimal(data.discountPercent) / Decimal('100')
    total = subtotal - discount_amount
    if total <= 0:
        raise HTTPException(422, 'The final amount must be greater than zero.')
    money = lambda value: f'{value.quantize(Decimal("0.01"))}'
    breakdown = (
        f'Course: US${money(data.courseUsd)} × {data.guestCount} guest(s) · '
        f'Interpreter service: US${money(data.interpreterUsd)} · '
        f'Other service adjustment: US${money(data.additionalUsd)} · '
        f'Subtotal: US${money(subtotal)} · Discount: {data.discountPercent}% '
        f'(−US${money(discount_amount)}) · '
        f'Course and interpreter fees are itemized separately.'
    )
    if data.note.strip():
        breakdown += f' · {data.note.strip()}'
    if PAYPAL_ADMIN_USE_CHECKOUT or not data.customerEmail:
        base_url = str(request.base_url).rstrip('/')
        order_payload = {
            'intent': 'CAPTURE',
            'purchase_units': [{
                'description': data.courseName.strip()[:127],
                'custom_id': f'MSB-{uuid.uuid4().hex[:18].upper()}',
                'amount': {'currency_code': 'USD', 'value': money(total)},
            }],
            'payment_source': {
                'paypal': {
                    'payment_method_preference': 'IMMEDIATE_PAYMENT_REQUIRED',
                    'experience_context': {
                        'brand_name': 'Midnight Sunrise Busan',
                        'landing_page': 'GUEST_CHECKOUT',
                        'shipping_preference': 'NO_SHIPPING',
                        'user_action': 'PAY_NOW',
                        'return_url': f'{base_url}/api/admin/orders/complete',
                        'cancel_url': f'{base_url}/api/admin/orders/cancelled',
                    }
                }
            },
        }
        try:
            with httpx.Client(timeout=25) as client:
                created = client.post(
                    f'{PAYPAL_API_BASE}/v2/checkout/orders',
                    headers=paypal_headers(f'admin-order-{uuid.uuid4()}'), json=order_payload,
                )
            if not created.is_success:
                logger.warning('PayPal admin order failed: status=%s body=%s', created.status_code, created.text[:1600])
                raise HTTPException(502, 'PayPal could not create this payment request.')
            created_data = created.json()
            order_id = created_data.get('id', '')
            payer_url = next(
                (link.get('href', '') for link in created_data.get('links', []) if link.get('rel') == 'payer-action'),
                '',
            ) or next(
                (link.get('href', '') for link in created_data.get('links', []) if link.get('rel') == 'approve'),
                '',
            )
            if not order_id or not payer_url:
                raise ValueError('PayPal did not return an order payment link')
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning('Admin PayPal order creation failed (%s).', type(exc).__name__)
            raise HTTPException(502, 'PayPal could not create this payment request.') from exc
        with closing(connect_db()) as db, db, db.cursor() as cursor:
            cursor.execute('''INSERT INTO admin_invoices
                (invoice_id, reservation_id, customer_name, customer_email, course_name,
                 total_amount, currency, payer_url, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (invoice_id) DO UPDATE SET 
                reservation_id = EXCLUDED.reservation_id, customer_name = EXCLUDED.customer_name,
                customer_email = EXCLUDED.customer_email, course_name = EXCLUDED.course_name,
                total_amount = EXCLUDED.total_amount, currency = EXCLUDED.currency,
                payer_url = EXCLUDED.payer_url, status = EXCLUDED.status''', (
                order_id, data.reservationId, data.customerName.strip(), data.customerEmail.strip(),
                data.courseName.strip(), money(total), 'USD', payer_url, 'created',
            ))
        send_telegram_alert(
            f'[결제 요청 생성] {order_id}\n고객: {data.customerName}\n'
            f'코스: {data.courseName}\n할인: {data.discountPercent}% '
            f'(US${money(discount_amount)})\n최종 청구: US${money(total)}\n{payer_url}'
        )
        return {
            'invoice_id': order_id, 'subtotal': money(subtotal),
            'discount_percent': data.discountPercent, 'discount_amount': money(discount_amount),
            'total': money(total), 'currency': 'USD',
            'payer_url': payer_url, 'qr_image': '', 'request_type': 'order',
        }
    
    recipient = {'billing_info': {'name': {'given_name': data.customerName.strip()[:140]}}}
    if data.customerEmail:
        recipient['billing_info']['email_address'] = data.customerEmail.strip()
    invoice_number = f'MSB-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:5].upper()}'
    items = []
    if data.courseUsd > 0:
        items.append({
            'name': data.courseName.strip(),
            'description': 'Course and venue arrangement',
            'quantity': str(data.guestCount),
            'unit_amount': {'currency_code': 'USD', 'value': money(data.courseUsd)},
            'unit_of_measure': 'QUANTITY',
        })
    if data.interpreterUsd > 0:
        items.append({
            'name': 'Interpreter service',
            'description': 'Professional interpreter service',
            'quantity': '1',
            'unit_amount': {'currency_code': 'USD', 'value': money(data.interpreterUsd)},
            'unit_of_measure': 'QUANTITY',
        })
    if data.additionalUsd > 0:
        items.append({
            'name': 'Additional agreed service',
            'quantity': '1',
            'unit_amount': {'currency_code': 'USD', 'value': money(data.additionalUsd)},
            'unit_of_measure': 'QUANTITY',
        })
    payload = {
        'detail': {
            'invoice_number': invoice_number,
            'invoice_date': date.today().isoformat(),
            'currency_code': 'USD',
            'note': breakdown,
            'payment_term': {'term_type': 'DUE_ON_RECEIPT'},
        },
        'invoicer': {'name': {'given_name': 'Midnight Sunrise', 'surname': 'Busan'}},
        'primary_recipients': [recipient],
        'items': items,
    }
    if data.discountPercent:
        payload['amount'] = {
            'breakdown': {
                'discount': {'invoice_discount': {'percent': str(data.discountPercent)}}
            }
        }
    try:
        with httpx.Client(timeout=25) as client:
            created = client.post(
                f'{PAYPAL_API_BASE}/v2/invoicing/invoices',
                headers=paypal_headers(f'invoice-{uuid.uuid4()}'), json=payload,
            )
            if not created.is_success:
                logger.warning('PayPal create invoice failed: status=%s body=%s', created.status_code, created.text[:1600])
                if created.status_code == 403:
                    raise HTTPException(502, 'PayPal Invoicing permission is not enabled for this account.')
                raise HTTPException(502, 'PayPal rejected the invoice details. Check the customer and amounts.')
            created_data = created.json()
            invoice_id = created_data.get('id', '')
            if not invoice_id:
                source = created_data.get('href', '') + ' ' + ' '.join(
                    link.get('href', '') for link in created_data.get('links', [])
                )
                match = re.search(r'INV2-[A-Z0-9-]+', source)
                invoice_id = match.group(0) if match else ''
            if not invoice_id:
                raise ValueError('PayPal did not return an invoice ID')
            sent = client.post(
                f'{PAYPAL_API_BASE}/v2/invoicing/invoices/{invoice_id}/send',
                headers=paypal_headers(f'send-{invoice_id}'),
                json={'send_to_recipient': bool(data.customerEmail), 'send_to_invoicer': False},
            )
            if not sent.is_success:
                logger.warning('PayPal send invoice failed: status=%s body=%s', sent.status_code, sent.text[:1600])
                if sent.status_code == 403:
                    raise HTTPException(502, 'PayPal Invoicing permission is not enabled for this account.')
                raise HTTPException(502, 'PayPal created the draft but could not activate its payment link.')
            sent_data = sent.json() if sent.content else {}
            links = sent_data.get('links', []) if isinstance(sent_data, dict) else []
            if isinstance(sent_data, dict) and sent_data.get('rel'):
                links.append(sent_data)
            payer_url = next((link.get('href', '') for link in links if link.get('rel') == 'payer-view'), '')
            invoice_detail = client.get(
                f'{PAYPAL_API_BASE}/v2/invoicing/invoices/{invoice_id}',
                headers=paypal_headers(),
            )
            if invoice_detail.is_success:
                detail_data = invoice_detail.json()
                payer_url = payer_url or detail_data.get('detail', {}).get('metadata', {}).get('recipient_view_url', '')
            qr = client.post(
                f'{PAYPAL_API_BASE}/v2/invoicing/invoices/{invoice_id}/generate-qr-code',
                headers=paypal_headers(), json={'width': 320, 'height': 320, 'action': 'pay'},
            )
            qr_image = qr.json().get('image', '') if qr.is_success else ''
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning('Admin invoice creation failed (%s).', type(exc).__name__)
        raise HTTPException(502, 'PayPal could not create this invoice.') from exc
    with closing(connect_db()) as db, db, db.cursor() as cursor:
        cursor.execute('''INSERT INTO admin_invoices
            (invoice_id, reservation_id, customer_name, customer_email, course_name,
             total_amount, currency, payer_url, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (invoice_id) DO UPDATE SET 
            reservation_id = EXCLUDED.reservation_id, customer_name = EXCLUDED.customer_name,
            customer_email = EXCLUDED.customer_email, course_name = EXCLUDED.course_name,
            total_amount = EXCLUDED.total_amount, currency = EXCLUDED.currency,
            payer_url = EXCLUDED.payer_url, status = EXCLUDED.status''', (
            invoice_id, data.reservationId, data.customerName.strip(), data.customerEmail.strip(),
            data.courseName.strip(), money(total), 'USD', payer_url, 'unpaid',
        ))
    send_telegram_alert(
        f'[인보이스 발행] {invoice_id}\n고객: {data.customerName}\n'
        f'코스: {data.courseName}\n할인: {data.discountPercent}% '
        f'(US${money(discount_amount)})\n최종 청구: US${money(total)}\n{payer_url}'
    )
    return {
        'invoice_id': invoice_id, 'subtotal': money(subtotal),
        'discount_percent': data.discountPercent, 'discount_amount': money(discount_amount),
        'total': money(total), 'currency': 'USD',
        'payer_url': payer_url, 'qr_image': qr_image, 'request_type': 'invoice',
    }


@app.get('/api/admin/orders/complete', response_class=HTMLResponse)
def complete_admin_order(token: str):
    if not re.fullmatch(r'[A-Z0-9]{8,32}', token):
        raise HTTPException(422, 'Invalid PayPal order ID.')
    with closing(connect_db()) as db, db.cursor() as cursor:
        cursor.execute('''SELECT invoice_id, customer_name, course_name, total_amount, currency, status
            FROM admin_invoices WHERE invoice_id = %s''', (token,))
        order = cursor.fetchone()
    if not order:
        raise HTTPException(404, 'Payment request not found.')
    if order['status'] != 'paid':
        try:
            with httpx.Client(timeout=25) as client:
                response = client.post(
                    f'{PAYPAL_API_BASE}/v2/checkout/orders/{token}/capture',
                    headers=paypal_headers(f'admin-capture-{token}'), json={},
                )
            if not response.is_success:
                logger.warning('PayPal admin capture failed: status=%s body=%s', response.status_code, response.text[:1600])
                raise HTTPException(502, 'PayPal could not confirm this payment.')
            payment = response.json()
            capture = payment['purchase_units'][0]['payments']['captures'][0]
            amount = capture['amount']
            if payment.get('status') != 'COMPLETED' or capture.get('status') != 'COMPLETED':
                raise ValueError('PayPal payment is not completed')
            if amount.get('currency_code') != order['currency'] or amount.get('value') != order['total_amount']:
                raise ValueError('PayPal payment amount does not match')
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning('Admin PayPal capture failed for %s (%s).', token, type(exc).__name__)
            raise HTTPException(502, 'PayPal could not confirm this payment.') from exc
        with closing(connect_db()) as db, db, db.cursor() as cursor:
            cursor.execute("UPDATE admin_invoices SET status = 'paid' WHERE invoice_id = %s", (token,))
        send_telegram_alert(
            f'[결제 완료] {token}\n고객: {order["customer_name"]}\n'
            f'코스: {order["course_name"]}\n결제 금액: US${order["total_amount"]}'
        )
    return HTMLResponse('''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Payment confirmed</title><body style="margin:0;background:#0d100e;color:#f7f1df;font-family:Arial,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center">
        <main style="max-width:520px;padding:40px"><div style="color:#d8b96d;letter-spacing:.18em;font-size:12px">MIDNIGHT SUNRISE BUSAN</div>
        <h1>Payment confirmed</h1><p style="color:#c8c9c4;line-height:1.6">Thank you. Your concierge team has been notified.<br>You may now return to WhatsApp.</p></main></body></html>''')


@app.get('/api/admin/orders/cancelled', response_class=HTMLResponse)
def cancelled_admin_order():
    return HTMLResponse('''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Payment not completed</title><body style="margin:0;background:#0d100e;color:#f7f1df;font-family:Arial,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center">
        <main style="max-width:520px;padding:40px"><div style="color:#d8b96d;letter-spacing:.18em;font-size:12px">MIDNIGHT SUNRISE BUSAN</div>
        <h1>Payment not completed</h1><p style="color:#c8c9c4;line-height:1.6">No payment was taken. Return to WhatsApp if you need help.</p></main></body></html>''')


@app.get('/admin')
def admin_page():
    return FileResponse(ROOT / 'admin.html')

@app.get('/')
def home():
    return FileResponse(ROOT / 'index.html')

@app.get('/{asset}')
def static_asset(asset: str):
    allowed = {'analytics.js', 'main.webp', 'mobile_main.webp', 'mainpic.webp', 'mainpic2.webp', 'mainpic3.webp', 'mainpic4.webp', 'mainpic5.webp', 'index.html', 'admin.html', 'guide.html', 'course-results.js', 'courses.css', 'api-config.js', 
               'booking-api.js', 'country-codes.js', 'google9b519aff934fd839.html', 'robots.txt', 'sitemap.xml', 'midnightbusan.png', 'hero-private-lounge-v1.png',
               'main pic1.png', 'main pic2.png', 'main pic3.png', 'main pic1.webp', 'main pic2.webp', 'main pic3.webp', 'main pic4.webp', 'main_pic5v2.webp', 
               'main pic7.webp', 'main_pic8.webp', 'main pic9.webp', 'main_pic10.webp', 'main pic11.webp', 'main_pic12.webp', 'main pic13.webp', 'main pic14.webp', 'main pic15.webp', 'main pic15.webp',
               'main pic16.webp', 'main pic17.webp', 'course-concept-600-v2.png', 
               'course-concept-800-v2.png', 'course-concept-1200.png'}
    if asset not in allowed and not re.fullmatch(r'(?:main|mobile_main|image1 \(\d+\))\.png', asset):
        raise HTTPException(404)
    path = ROOT / asset
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.getenv('BUSAN_HOST', '127.0.0.1'), port=int(os.getenv('PORT', os.getenv('BUSAN_PHONE_PORT', '8001'))))
