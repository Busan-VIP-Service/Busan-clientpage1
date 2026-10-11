from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import MagicMock, patch
import unittest
import main


class DatabaseInitializationTests(unittest.TestCase):
    def setUp(self):
        for name, value in [('DATABASE_URL', 'test-only'), ('_schema_ready', False), ('_schema_lock', Lock())]:
            change = patch.object(main, name, value)
            change.start()
            self.addCleanup(change.stop)

    def test_parallel_first_requests_initialize_once(self):
        barrier = Barrier(6)
        def open_connection(*args, **kwargs):
            barrier.wait(timeout=5)
            return MagicMock()
        with patch.object(main.psycopg2, 'connect', side_effect=open_connection), \
                patch.object(main, '_initialize_schema') as initialize:
            with ThreadPoolExecutor(max_workers=6) as pool:
                connections = list(pool.map(lambda _: main.connect_db(), range(6)))
        self.assertEqual(len(connections), 6)
        initialize.assert_called_once()
        self.assertTrue(main._schema_ready)

    def test_failed_initialization_closes_connection_and_retries(self):
        failed, success = MagicMock(), MagicMock()
        with patch.object(main.psycopg2, 'connect', side_effect=[failed, success]), \
                patch.object(main, '_initialize_schema', side_effect=[RuntimeError('unavailable'), None]):
            with self.assertRaises(RuntimeError):
                main.connect_db()
            failed.close.assert_called_once()
            self.assertFalse(main._schema_ready)
            self.assertIs(main.connect_db(), success)
            self.assertTrue(main._schema_ready)
            success.close.assert_not_called()

    def test_regular_requests_do_not_run_schema_ddl(self):
        with patch.object(main, '_schema_ready', True), patch.object(main.psycopg2, 'connect') as connect:
            main.connect_db()
            main.analytics_db()
            connect.return_value.cursor.assert_not_called()
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(connect.call_args.kwargs['connect_timeout'], 10)

    def test_migrations_lock_before_ddl_and_commit_together(self):
        db = MagicMock()
        cursor = db.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        main._initialize_schema(db)
        queries = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(queries[0], 'SELECT pg_advisory_xact_lock(%s)')
        self.assertTrue(any('CREATE TABLE IF NOT EXISTS reservations' in q for q in queries))
        self.assertTrue(any('CREATE TABLE IF NOT EXISTS page_views' in q for q in queries))
        db.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
