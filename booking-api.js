window.busanReservationUrl = function () {
    const configured = (window.BUSAN_API_BASE_URL || '').trim();
    if (!configured) {
        if (location.protocol === 'file:' || location.hostname.endsWith('.github.io')) {
            throw new Error('The booking service is not connected yet. Please contact the concierge.');
        }
        return '/api/reservation';
    }
    const base = new URL(configured);
    if (!['https:', 'http:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
        throw new Error('The booking service address is invalid.');
    }
    if (location.protocol === 'https:' && base.protocol !== 'https:') {
        throw new Error('The booking service requires a secure connection.');
    }
    return base.href.replace(/\/$/, '') + '/api/reservation';
};
