// No cookies, visitor identifiers, IP addresses, or full URLs are stored.
(() => {
  const send = () => {
    const params = new URLSearchParams(location.search);
    let source = 'direct';
    try {
      const ref = new URL(document.referrer);
      if (ref.origin !== location.origin) {
        source = /(^|\.)(google\.[a-z.]+|bing\.com|search\.naver\.com|search\.daum\.net|duckduckgo\.com)$/.test(ref.hostname) ? 'search' : 'referral';
      }
    } catch (_) { /* No referrer is normal for direct visits. */ }
    if ((params.get('utm_source') || '').toLowerCase() === 'qr' || (params.get('utm_medium') || '').toLowerCase() === 'qr') source = 'qr';
    const event_id = crypto.randomUUID();
    const base = (window.BUSAN_API_BASE_URL || '').replace(/\/$/, '');
    const submit = () => fetch(base + '/api/analytics/page-view', {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: {'Content-Type': 'application/json', 'x-busan-request': '1'},
      body: JSON.stringify({event_id, source})
    });
    submit().catch(() => setTimeout(() => submit().catch(() => {}), 2000));
  };
  if (document.visibilityState === 'prerender' || document.prerendering) {
    document.addEventListener('prerenderingchange', send, {once: true});
  } else { send(); }
})();
