from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
import main


class AnalyticsTests(unittest.TestCase):
    def test_collection_access_and_totals(self):
        with TemporaryDirectory() as directory, patch.object(main, 'DB_PATH', Path(directory) / 'test.db'), patch.object(main, 'ADMIN_PASSWORD', 'test-password'), TestClient(main.app) as client:
            payload = {'event_id': 'a' * 32, 'source': 'qr'}
            self.assertEqual(client.get('/api/admin/analytics').status_code, 401)
            self.assertEqual(client.post('/api/analytics/page-view', json=payload).status_code, 400)
            headers = {'x-busan-request': '1'}
            for _ in range(2):
                self.assertEqual(client.post('/api/analytics/page-view', json=payload, headers=headers).status_code, 204)
            client.post('/api/analytics/page-view', json={**payload, 'event_id': 'b' * 32, 'source': 'direct'}, headers=headers)
            client.post('/api/analytics/page-view', json={**payload, 'event_id': 'c' * 32}, headers={**headers, 'user-agent': 'Googlebot'})
            today = datetime.now(timezone(timedelta(hours=9))).date()
            with closing(main.analytics_db()) as db, db:
                db.execute('INSERT INTO page_views VALUES (?, ?, ?)', ('old', (today-timedelta(days=7)).isoformat(), 'search'))
            client.cookies.set(main.ADMIN_COOKIE, main.issue_admin_session())
            client.post('/api/analytics/page-view', json={**payload, 'event_id': 'd' * 32}, headers=headers)
            result = client.get('/api/admin/analytics')
            self.assertEqual(result.headers['cache-control'], 'no-store')
            data = result.json()
            self.assertEqual((data['total'], data['today'], data['week']), (3, 2, 2))
            self.assertEqual(data['sources'], {'qr': 1, 'direct': 1, 'search': 1})
            self.assertEqual(len(data['days']), 30)
            self.assertEqual(data['days'][0], {'day': today.isoformat(), 'views': 2})
            self.assertEqual(client.get('/analytics.js').status_code, 200)
            self.assertIn('analytics.js', client.get('/').text)


if __name__ == '__main__':
    unittest.main()
