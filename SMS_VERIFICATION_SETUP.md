# SOLAPI SMS verification setup

The server generates a six-digit one-time code and verifies it against a server-side HMAC digest. SOLAPI only sends the SMS. No code is accepted without a real send attempt and matching entry. A booking requires a verified, unused token for the same phone number.

Set these **server-side** environment variables:

- `SOLAPI_API_KEY` and `SOLAPI_API_SECRET`: SOLAPI API credentials. Keep them off the website and out of Git.
- `SOLAPI_SENDER`: a sender number registered and approved in SOLAPI.
- `SOLAPI_OTP_SECRET`: an independently generated random secret of at least 32 characters. Keep stable across deployments; changing it invalidates outstanding codes.
- `DATABASE_URL`: PostgreSQL connection URL.
- `BUSAN_ALLOWED_ORIGINS`: exact HTTPS origins allowed to call the API.

The API needs a deployed server, database, funded SOLAPI account, approved sender and actual destination coverage. A static GitHub Pages site must set `BUSAN_API_BASE_URL` in `api-config.js` to the public HTTPS API URL. Until configured, code requests return 503. A successful provider API response means the message was accepted for sending, not that the customer received it. Test real delivery to each important destination country before launch. SOLAPI notes special limits for some destinations, including Canada and Singapore.

The server limits requests to 5 per phone and 15 per IP per hour, with a 60-second resend interval. Codes expire in 10 minutes and are limited to 5 checks. Do not log codes or API secrets. Verify a test booking has `phone_verified_at` populated.

Official references: https://github.com/solapi/solapi-python/blob/main/examples/simple/send_sms.py and https://guide.solapi.com/intl-pricing
