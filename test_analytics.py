from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import sqlite3
import unittest
from fastapi.testclient import TestClient
import main


class AdminTests(unittest.TestCase):
    def setUp(self):
        main.ADMIN_LOGIN_ATTEMPTS.clear()
        p = patch.object(main, 'ADMIN_PASSWORD', '886223')
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def login(self):
        response = self.client.post('/api/admin/login', json={'password': '886223'})
        self.assertEqual(response.status_code, 200)

    def test_auth_and_logout(self):
        self.assertEqual(self.client.get('/api/admin/session').status_code, 401)
        self.assertEqual(self.client.post('/api/admin/login', json={'password': 'wrong'}).status_code, 401)
        self.login()
        self.assertEqual(self.client.get('/api/admin/session').status_code, 200)
        self.assertEqual(self.client.post('/api/admin/logout').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/session').status_code, 401)

    def test_actual_analytics(self):
        actual = {'total': 42, 'today': 2, 'week': 8, 'sources': {'qr': 1},
                  'days': [], 'hours': [{'hour': 18, 'views': 2}], 'unknown_today': 0}
        with patch.object(main, 'real_analytics_data', return_value=actual) as read:
            self.assertEqual(self.client.get('/api/admin/analytics').status_code, 401)
            read.assert_not_called()
            self.login()
            response = self.client.get('/api/admin/analytics')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json(), {**actual, 'display_mode': 'actual'})

    def analytics_fixture(self):
        connection = sqlite3.connect(':memory:', check_same_thread=False)
        self.addCleanup(connection.close)
        connection.row_factory = lambda cursor, row: dict(zip([c[0] for c in cursor.description], row))
        connection.execute('CREATE TABLE page_views (day TEXT, source TEXT, viewed_at TEXT)')
        connection.executemany('INSERT INTO page_views VALUES (?, ?, ?)', [
            ('2026-10-17', 'direct', '2026-10-16 15:30:00'),
            ('2026-10-16', 'qr', '2026-10-15 15:10:00'),
            ('2026-10-16', 'direct', '2026-10-16 14:50:00'),
            ('2026-10-16', 'direct', '2026-10-16 14:59:59'),
            ('2026-10-16', 'search', None),
            ('2026-09-01', 'referral', '2026-09-01 03:00:00'),
        ])
        raw_cursor = connection.cursor()
        db = MagicMock()
        cursor = db.cursor.return_value.__enter__.return_value
        def execute(sql, values):
            sql = sql.replace("EXTRACT(HOUR FROM viewed_at AT TIME ZONE 'Asia/Seoul')::int",
                              "CAST(strftime('%H', viewed_at, '+9 hours') AS INTEGER)")
            return raw_cursor.execute(sql.replace('%s', '?'), values)
        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = raw_cursor.fetchone
        cursor.fetchall.side_effect = raw_cursor.fetchall
        return db

    def test_historical_analytics_include_legacy_counts_and_korean_hours(self):
        self.login()
        db = self.analytics_fixture()
        with patch.object(main, 'analytics_db', return_value=db), patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 17, 0, 15, tzinfo=timezone(timedelta(hours=9)))
            response = self.client.get('/api/admin/analytics?day=2026-10-16')
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data['today_date'], data['selected_day']), ('2026-10-17', '2026-10-16'))
        self.assertEqual((data['today'], data['week'], data['total']), (1, 5, 6))
        self.assertEqual((data['selected_total'], data['unknown_hours'], data['unknown_today']), (4, 1, 0))
        self.assertEqual(len(data['hours']), 24)
        self.assertEqual(data['hours'][0]['views'], 1)
        self.assertEqual(data['hours'][23]['views'], 2)
        self.assertEqual(data['hours'][1]['views'], 0)
        self.assertEqual(sum(h['views'] for h in data['hours']) + data['unknown_hours'], data['selected_total'])
        self.assertEqual((data['range_start'], data['range_end']), ('2026-09-17', '2026-10-16'))
        self.assertEqual(data['days'][0], {'day': '2026-10-16', 'views': 4})
        self.assertEqual(sum(d['views'] for d in data['days']), 4)
        self.assertEqual(data['sources'], {'direct': 2, 'qr': 1, 'search': 1})
        self.assertEqual(data['started'], '2026-09-01')

    def test_history_older_than_thirty_days_and_empty_days(self):
        self.login()
        db = self.analytics_fixture()
        with patch.object(main, 'analytics_db', return_value=db), patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=9)))
            old = self.client.get('/api/admin/analytics?day=2026-09-01').json()
            empty = self.client.get('/api/admin/analytics?day=2026-09-02').json()
            today = self.client.get('/api/admin/analytics').json()
        self.assertEqual(old['selected_total'], 1)
        self.assertEqual(old['hours'][12]['views'], 1)
        self.assertEqual(len(old['days']), 30)
        self.assertEqual(empty['selected_total'], 0)
        self.assertTrue(all(h['views'] == 0 for h in empty['hours']))
        self.assertEqual(today['selected_day'], '2026-10-17')
        self.assertEqual(today['hours'][0]['views'], 1)

    def test_invalid_and_future_analytics_dates_do_not_read_database(self):
        self.login()
        with patch.object(main, 'analytics_db') as connect, patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=9)))
            for day in ['not-a-date', '2026-02-30']:
                self.assertEqual(self.client.get('/api/admin/analytics', params={'day': day}).status_code, 422)
            self.assertEqual(self.client.get('/api/admin/analytics?day=2026-10-18').status_code, 400)
            connect.assert_not_called()

    def test_legacy_only_and_no_recorded_views(self):
        self.login()
        db = self.analytics_fixture()
        cursor = db.cursor.return_value.__enter__.return_value
        cursor.execute('UPDATE page_views SET viewed_at = NULL WHERE day = %s', ('2026-10-16',))
        with patch.object(main, 'analytics_db', return_value=db), patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=9)))
            legacy = self.client.get('/api/admin/analytics?day=2026-10-16').json()
            cursor.execute('DELETE FROM page_views', ())
            empty = self.client.get('/api/admin/analytics').json()
        self.assertEqual((legacy['selected_total'], legacy['unknown_hours']), (4, 4))
        self.assertTrue(all(h['views'] == 0 for h in legacy['hours']))
        self.assertEqual((empty['total'], empty['selected_total'], empty['unknown_hours']), (0, 0, 0))
        self.assertIsNone(empty['started'])
        self.assertTrue(all(d['views'] == 0 for d in empty['days']))

    def test_reservations(self):
        db = MagicMock()
        db.cursor.return_value.__enter__.return_value.fetchall.return_value = [{'id': 7, 'name': 'Test guest'}]
        with patch.object(main, 'connect_db', return_value=db) as connect:
            self.assertEqual(self.client.get('/api/admin/reservations').status_code, 401)
            connect.assert_not_called()
            self.login()
            response = self.client.get('/api/admin/reservations')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reservations'][0]['id'], 7)

    def test_qr_collection_excludes_admin(self):
        with patch.object(main, 'analytics_db') as connect:
            cursor = connect.return_value.cursor.return_value.__enter__.return_value
            payload = {'event_id': 'test-qr-event-12345', 'source': 'qr'}
            self.assertEqual(self.client.post('/api/analytics/page-view', json=payload).status_code, 400)
            headers = {'x-busan-request': '1'}
            self.assertEqual(self.client.post('/api/analytics/page-view', json=payload, headers=headers).status_code, 204)
            sql, values = cursor.execute.call_args.args
            self.assertIn('ON CONFLICT (event_id) DO NOTHING', sql)
            self.assertEqual(values[2], 'qr')
            connect.reset_mock()
            self.login()
            self.assertEqual(self.client.post('/api/analytics/page-view', json=payload, headers=headers).status_code, 204)
            connect.assert_not_called()

    def test_retired_admin(self):
        for path in ['/admin-new', '/admin-new.html']:
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers['location'], '/admin')
        self.assertEqual(self.client.post('/api/admin-new/login', json={'password': 'old'}).status_code, 404)
        self.client.cookies.set('busan_new_admin_session', 'retired-session')
        self.assertEqual(self.client.get('/api/admin/session').status_code, 401)

    def test_telegram_check_mocked(self):
        with patch.object(main, 'send_telegram_alert', return_value={'ok': True}) as send:
            self.assertEqual(self.client.post('/api/admin/telegram/test').status_code, 401)
            send.assert_not_called()
            self.login()
            self.assertEqual(self.client.post('/api/admin/telegram/test').status_code, 403)
            response = self.client.post('/api/admin/telegram/test', headers={'x-admin-request': '1'})
            self.assertEqual(response.json(), {'sent': True})
            send.assert_called_once()

    def test_payment_and_discount_mocked(self):
        payload = {'customerName': 'Test guest', 'courseName': 'Eco Course', 'courseUsd': 360,
                   'guestCount': 2, 'interpreterUsd': 150, 'additionalUsd': 30, 'discountPercent': 20}
        self.assertEqual(self.client.post('/api/admin/invoices', json=payload).status_code, 401)
        self.login()
        provider = MagicMock()
        provider.post.return_value.is_success = True
        provider.post.return_value.json.return_value = {'id': 'TESTORDER123',
            'links': [{'rel': 'payer-action', 'href': 'https://example.com/payment'}]}
        with patch.object(main.httpx, 'Client') as factory, \
                patch.object(main, 'paypal_headers', return_value={}), \
                patch.object(main, 'PAYPAL_ADMIN_USE_CHECKOUT', True), \
                patch.object(main, 'connect_db'), patch.object(main, 'send_telegram_alert'):
            factory.return_value.__enter__.return_value = provider
            response = self.client.post('/api/admin/invoices', json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data['subtotal'], data['discount_amount'], data['total']), ('900.00', '180.00', '720.00'))
        self.assertEqual(data['payer_url'], 'https://example.com/payment')
        self.assertEqual(provider.post.call_args.kwargs['json']['purchase_units'][0]['amount']['value'], '720.00')

    def test_admin_page(self):
        page = self.client.get('/admin').text
        for feature in ['viewHours', 'viewSources', 'viewDays', 'reservations', 'discountPercent',
                        'guestCount', 'invoiceButton', 'telegramTestButton', 'whatsappLink', 'id="qr"']:
            self.assertIn(feature, page)
        self.assertIn("const authBase='/api/admin'", page)
        self.assertIn('maxlength="6"', page)
        self.assertNotIn('isNewAdmin', page)
        self.assertNotIn('/api/admin-new', page)


if __name__ == '__main__':
    unittest.main()
