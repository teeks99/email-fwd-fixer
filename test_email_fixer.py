import unittest
import email
from email.header import Header
from email_fixer import decode_header_value, extract_message_details, extract_message_id

class TestEmailFixer(unittest.TestCase):

    def test_decode_plain_header(self):
        self.assertEqual(decode_header_value("Simple Subject"), "Simple Subject")
        self.assertEqual(decode_header_value("John Doe <john@example.com>"), "John Doe <john@example.com>")

    def test_decode_encoded_header(self):
        encoded = Header("Test Subject Unicode ⭐", "utf-8").encode()
        self.assertEqual(decode_header_value(encoded), "Test Subject Unicode ⭐")

    def test_decode_multiline_header(self):
        multiline = "Line 1\r\n Line 2\n\tLine 3"
        self.assertEqual(decode_header_value(multiline), "Line 1 Line 2 Line 3")

    def test_extract_message_details_complete(self):
        raw_email = (
            b"Message-ID: <msg123@example.com>\r\n"
            b"From: Alice <alice@example.com>\r\n"
            b"Subject: Important Update\r\n\r\n"
        )
        details = extract_message_details(raw_email)
        self.assertEqual(details['message_id'], "<msg123@example.com>")
        self.assertEqual(details['sender'], "Alice <alice@example.com>")
        self.assertEqual(details['subject'], "Important Update")

    def test_extract_message_details_missing_fields(self):
        raw_email = b"X-Custom-Header: True\r\n\r\n"
        details = extract_message_details(raw_email)
        self.assertEqual(details['message_id'], "")
        self.assertEqual(details['sender'], "Unknown Sender")
        self.assertEqual(details['subject'], "No Subject")
        self.assertEqual(extract_message_id(raw_email), "")

    HEADER_WITH_ID = b"Message-ID: <test1234@example.com>\r\nFrom: sender@test.com\r\nSubject: Hello\r\n"
    HEADER_WITHOUT_ID = b"From: sender@test.com\r\nSubject: Hello\r\n"

    @staticmethod
    def make_passthrough(header, fetch_full=('OK', [(None, b"full raw email content")])):
        """A PassThrough whose replies depend on the command, not on call order."""
        from unittest.mock import MagicMock

        def respond(*args, **kwargs):
            if args[:2] == ('uid', 'FETCH'):
                return ('OK', [(None, header)]) if 'HEADER.FIELDS' in args[3] else fetch_full
            return ('OK', [b''])

        mock_passthrough = MagicMock()
        mock_passthrough.execute.side_effect = respond
        return mock_passthrough

    @staticmethod
    def deleted(mock_passthrough):
        return any(
            call.args[:2] == ('uid', 'STORE') and '\\Deleted' in call.args
            for call in mock_passthrough.execute.call_args_list
        )

    def make_fixer(self, mock_passthrough, mock_notify, mock_gmail=None, config=None, stats=None):
        from unittest.mock import MagicMock
        from email_fixer import ForwardFixer, Config

        config = config or Config(300, False, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        fixer = ForwardFixer(config, stats if stats is not None else {'checked': 0, 'notified': 0})
        fixer.passthrough = mock_passthrough
        fixer.gmail = mock_gmail if mock_gmail is not None else MagicMock()
        fixer.notify = mock_notify
        return fixer

    def run_handle(self, mock_passthrough, mock_notify, found_in_gmail=False,
                   mock_gmail=None, config=None, stats=None):
        from unittest.mock import patch
        from email_fixer import ForwardFixer

        fixer = self.make_fixer(mock_passthrough, mock_notify, mock_gmail, config, stats)
        with patch.object(ForwardFixer, '_gmail_has', return_value=found_in_gmail):
            fixer.handle_message(b'1')
        return fixer.stats

    def test_handle_message_not_found_logging(self):
        from unittest.mock import MagicMock
        from email_fixer import logger

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b'[APPENDUID 1 1] Append completed'])

        with self.assertLogs(logger, level='INFO') as log_cm:
            stats = self.run_handle(mock_passthrough, mock_notify)
        self.assertTrue(any("Message <test1234@example.com> NOT found in GMail" in msg for msg in log_cm.output))

        self.assertEqual(stats['notified'], 1)
        self.assertEqual(stats['checked'], 1)
        self.assertTrue(self.deleted(mock_passthrough))

    def test_found_in_gmail_is_deleted_without_copying(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID, fetch_full=('OK', [b'']))
        mock_notify = MagicMock()
        stats = self.run_handle(mock_passthrough, mock_notify, found_in_gmail=True)

        mock_notify.execute.assert_not_called()
        self.assertEqual(stats['notified'], 0)
        self.assertTrue(self.deleted(mock_passthrough))

    def test_not_deleted_when_notify_append_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('NO', [b'Over quota'])
        stats = self.run_handle(mock_passthrough, mock_notify)

        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_not_deleted_when_full_fetch_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID, fetch_full=('NO', [None]))
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        stats = self.run_handle(mock_passthrough, mock_notify)

        mock_notify.execute.assert_not_called()
        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_not_deleted_when_header_fetch_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = MagicMock()
        mock_passthrough.execute.return_value = ('NO', [None])
        stats = self.run_handle(mock_passthrough, MagicMock())

        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats, {'checked': 0, 'notified': 0})

    def test_missing_message_id_not_deleted_when_copy_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITHOUT_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('NO', [b'Over quota'])
        stats = self.run_handle(mock_passthrough, mock_notify)

        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_missing_message_id_deleted_once_copied(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITHOUT_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        stats = self.run_handle(mock_passthrough, mock_notify)

        self.assertTrue(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 1)

    def test_missing_message_id_skips_the_gmail_lookup(self):
        """No Message-ID means nothing to search for -- copy it and move on."""
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer

        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        fixer = self.make_fixer(self.make_passthrough(self.HEADER_WITHOUT_ID), mock_notify)

        with patch.object(ForwardFixer, '_gmail_has') as mock_lookup:
            fixer.handle_message(b'1')

        mock_lookup.assert_not_called()
        self.assertEqual(fixer.stats['notified'], 1)

    def test_gmail_inbox_copy_failure_still_deletes(self):
        from unittest.mock import MagicMock
        from email_fixer import Config

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        mock_gmail = MagicMock()
        mock_gmail.execute.return_value = ('NO', [b'Over quota'])
        config = Config(300, True, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        stats = self.run_handle(mock_passthrough, mock_notify, mock_gmail=mock_gmail, config=config)

        self.assertTrue(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 1)

    def test_gmail_inbox_copy_reuses_the_single_body_fetch(self):
        """Both destinations share one FETCH -- splitting the copies must not
        turn into fetching the same message once per destination."""
        from unittest.mock import MagicMock
        from email_fixer import Config

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        mock_gmail = MagicMock()
        mock_gmail.execute.return_value = ('OK', [b''])
        config = Config(300, True, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        self.run_handle(mock_passthrough, mock_notify, mock_gmail=mock_gmail, config=config)

        body_fetches = [c for c in mock_passthrough.execute.call_args_list
                        if c.args[:2] == ('uid', 'FETCH') and c.args[3] == '(RFC822)']
        self.assertEqual(1, len(body_fetches))

        notified = [c.args[4] for c in mock_notify.execute.call_args_list if c.args[0] == 'append']
        mirrored = [c.args[4] for c in mock_gmail.execute.call_args_list if c.args[0] == 'append']
        self.assertEqual([b"full raw email content"], notified)
        self.assertEqual(notified, mirrored)

    def test_no_gmail_inbox_copy_when_not_configured(self):
        from unittest.mock import MagicMock

        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        mock_gmail = MagicMock()
        stats = self.run_handle(self.make_passthrough(self.HEADER_WITH_ID), mock_notify,
                                mock_gmail=mock_gmail)

        mock_gmail.execute.assert_not_called()
        self.assertEqual(stats['notified'], 1)

    def test_no_gmail_inbox_copy_when_notify_copy_failed(self):
        """The GMail Inbox copy is a mirror of a secured message, so it must not
        happen for a message that was never secured."""
        from unittest.mock import MagicMock
        from email_fixer import Config

        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('NO', [b'Over quota'])
        mock_gmail = MagicMock()
        config = Config(300, True, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        stats = self.run_handle(mock_passthrough, mock_notify, mock_gmail=mock_gmail, config=config)

        mock_gmail.execute.assert_not_called()
        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_passthrough_addresses_messages_by_uid(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        self.run_handle(mock_passthrough, mock_notify)

        calls = [call.args for call in mock_passthrough.execute.call_args_list]
        self.assertIn(('uid', 'FETCH', b'1', '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])'), calls)
        self.assertIn(('uid', 'FETCH', b'1', '(RFC822)'), calls)
        self.assertIn(('uid', 'STORE', b'1', '+FLAGS', '\\Deleted'), calls)
        # Nothing may address a message by sequence number, which shifts on expunge.
        self.assertEqual([], [c for c in calls if c[0] in ('fetch', 'store', 'search')])

    def test_run_once_searches_by_uid(self):
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer, Config

        passthrough = MagicMock()
        passthrough.is_connected.return_value = True
        passthrough.execute.return_value = ('OK', [b''])
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")

        with patch('email_fixer.IMAPClient') as mock_cls:
            mock_cls.return_value = passthrough
            ForwardFixer(config, {'checked': 0, 'notified': 0}).run_once()

        passthrough.execute.assert_called_once_with('uid', 'SEARCH', None, 'ALL')

    def test_run_once_handles_every_uid_then_expunges_once(self):
        from unittest.mock import MagicMock, call, patch
        from email_fixer import ForwardFixer, Config

        passthrough = MagicMock()
        passthrough.is_connected.return_value = True
        passthrough.execute.return_value = ('OK', [b'3 7 11'])
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")
        fixer = ForwardFixer(config, {'checked': 0, 'notified': 0})

        with patch('email_fixer.IMAPClient') as mock_cls, \
             patch.object(ForwardFixer, 'handle_message') as mock_handle, \
             patch.object(ForwardFixer, '_expunge_passthrough') as mock_expunge:
            mock_cls.return_value = passthrough
            fixer.run_once()

        self.assertEqual([call(b'3'), call(b'7'), call(b'11')], mock_handle.call_args_list)
        mock_expunge.assert_called_once()

    def test_run_once_keeps_going_when_one_message_raises(self):
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer, Config

        passthrough = MagicMock()
        passthrough.is_connected.return_value = True
        passthrough.execute.return_value = ('OK', [b'1 2 3'])
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")
        fixer = ForwardFixer(config, {'checked': 0, 'notified': 0})

        with patch('email_fixer.IMAPClient') as mock_cls, \
             patch.object(ForwardFixer, 'handle_message', side_effect=[None, RuntimeError('boom'), None]) as mock_handle, \
             patch.object(ForwardFixer, '_expunge_passthrough') as mock_expunge:
            mock_cls.return_value = passthrough
            fixer.run_once()

        self.assertEqual(3, mock_handle.call_count)
        mock_expunge.assert_called_once()

    def test_run_once_skips_gmail_and_notify_when_passthrough_is_empty(self):
        """An idle poll should cost one login, not three."""
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer, Config

        passthrough = MagicMock()
        passthrough.is_connected.return_value = True
        passthrough.execute.return_value = ('OK', [b''])
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")

        with patch('email_fixer.IMAPClient') as mock_cls:
            mock_cls.return_value = passthrough
            ForwardFixer(config, {'checked': 0, 'notified': 0}).run_once()

        self.assertEqual(1, mock_cls.call_count)

    def test_run_once_closes_every_connection_it_opened(self):
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer, Config

        clients = []

        def make_client(*args, **kwargs):
            client = MagicMock()
            client.is_connected.return_value = True
            client.execute.return_value = ('OK', [b'1'])
            clients.append(client)
            return client

        config = Config(300, False, "imap.test", 993, "u", "p", "gm", 993, "u", "p", "nt", 993, "u", "p")
        with patch('email_fixer.IMAPClient', side_effect=make_client), \
             patch.object(ForwardFixer, 'handle_message'):
            ForwardFixer(config, {'checked': 0, 'notified': 0}).run_once()

        self.assertEqual(3, len(clients))
        for client in clients:
            client.close.assert_called_once()

    def test_run_once_closes_connections_when_the_pass_fails(self):
        """try/finally, not the happy path, is what guarantees teardown."""
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer, Config, logger

        passthrough = MagicMock()
        passthrough.is_connected.return_value = True
        passthrough.execute.side_effect = RuntimeError('server exploded')
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")

        with patch('email_fixer.IMAPClient') as mock_cls, \
             self.assertLogs(logger, level='ERROR') as log_cm:
            mock_cls.return_value = passthrough
            ForwardFixer(config, {'checked': 0, 'notified': 0}).run_once()

        passthrough.close.assert_called_once()
        self.assertTrue(any("Error processing PassThrough" in msg for msg in log_cm.output))

    def test_run_once_closes_passthrough_when_login_fails(self):
        from unittest.mock import MagicMock, patch
        from email_fixer import ForwardFixer, Config

        passthrough = MagicMock()
        passthrough.is_connected.return_value = False
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")

        with patch('email_fixer.IMAPClient') as mock_cls:
            mock_cls.return_value = passthrough
            ForwardFixer(config, {'checked': 0, 'notified': 0}).run_once()

        self.assertEqual(1, mock_cls.call_count)
        passthrough.close.assert_called_once()
        passthrough.execute.assert_not_called()

    @staticmethod
    def stale_and_fresh():
        from unittest.mock import MagicMock

        stale = MagicMock()
        stale.state = 'SELECTED'
        fresh = MagicMock()
        fresh.state = 'SELECTED'
        fresh.select.return_value = ('OK', [b'1'])
        return stale, fresh

    def test_reconnect_logs_out_without_expunging(self):
        from unittest.mock import patch
        from email_fixer import IMAPClient

        stale, fresh = self.stale_and_fresh()
        with patch('email_fixer.imaplib.IMAP4_SSL', return_value=fresh):
            client = IMAPClient('imap.test', 993, 'u', 'p', folder='INBOX')
            client.mail = stale
            client.current_folder = 'INBOX'
            self.assertTrue(client.connect())

        # IMAP CLOSE would purge every message already flagged \Deleted this pass.
        stale.close.assert_not_called()
        stale.logout.assert_called_once()
        self.assertIs(client.mail, fresh)

    def test_execute_reconnect_after_imap_error_does_not_expunge(self):
        import imaplib
        from unittest.mock import patch
        from email_fixer import IMAPClient

        stale, fresh = self.stale_and_fresh()
        # A NO/BAD reply leaves the socket alive, so CLOSE here really would expunge.
        stale.noop.side_effect = imaplib.IMAP4.error('BAD stale session')
        fresh.noop.return_value = ('OK', [b'NOOP completed'])

        with patch('email_fixer.imaplib.IMAP4_SSL', return_value=fresh):
            client = IMAPClient('imap.test', 993, 'u', 'p', folder='INBOX')
            client.mail = stale
            client.current_folder = 'INBOX'
            self.assertEqual(client.execute('noop'), ('OK', [b'NOOP completed']))

        stale.close.assert_not_called()
        self.assertEqual('INBOX', client.current_folder)

    def test_close_still_expunges_on_normal_teardown(self):
        from unittest.mock import MagicMock
        from email_fixer import IMAPClient

        mail = MagicMock()
        mail.state = 'SELECTED'
        client = IMAPClient('imap.test', 993, 'u', 'p', folder='INBOX')
        client.mail = mail
        client.close()

        mail.close.assert_called_once()
        mail.logout.assert_called_once()
        self.assertIsNone(client.mail)

if __name__ == "__main__":
    unittest.main()
