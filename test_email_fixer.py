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
    def make_passthrough(header, fetch_full=('OK', [(None, b"full raw email content")]), noop=True):
        from unittest.mock import MagicMock
        mock_passthrough = MagicMock()
        responses = [('OK', [(None, header)])]
        if noop:
            responses.append(('OK', [b'']))
        responses.append(fetch_full)
        responses.append(('OK', [b'']))
        mock_passthrough.execute.side_effect = responses
        return mock_passthrough

    @staticmethod
    def deleted(mock_passthrough):
        return any(
            call.args[:2] == ('uid', 'STORE') and '\\Deleted' in call.args
            for call in mock_passthrough.execute.call_args_list
        )

    def run_process(self, mock_passthrough, mock_notify, found_in_gmail=False, config=None, stats=None):
        from unittest.mock import MagicMock, patch
        from email_fixer import process_single_message, Config

        config = config or Config(300, False, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        stats = stats if stats is not None else {'checked': 0, 'notified': 0}
        with patch('email_fixer.check_gmail_for_message', return_value=found_in_gmail), \
             patch('email_fixer.time.sleep'):
            process_single_message(mock_passthrough, MagicMock(), mock_notify, b'1', stats, config)
        return stats

    def test_process_single_message_not_found_logging(self):
        from unittest.mock import MagicMock, patch
        from email_fixer import process_single_message, Config, logger

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_gmail = MagicMock()
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b'[APPENDUID 1 1] Append completed'])
        config = Config(300, False, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        stats = {'checked': 0, 'notified': 0}

        with patch('email_fixer.check_gmail_for_message', return_value=False), \
             patch('email_fixer.time.sleep'), \
             self.assertLogs(logger, level='INFO') as log_cm:
            process_single_message(mock_passthrough, mock_gmail, mock_notify, b'1', stats, config)
            self.assertTrue(any("Message <test1234@example.com> NOT found in GMail" in msg for msg in log_cm.output))

        self.assertEqual(stats['notified'], 1)
        self.assertTrue(self.deleted(mock_passthrough))

    def test_found_in_gmail_is_deleted_without_copying(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID, fetch_full=('OK', [b'']))
        mock_notify = MagicMock()
        stats = self.run_process(mock_passthrough, mock_notify, found_in_gmail=True)

        mock_notify.execute.assert_not_called()
        self.assertEqual(stats['notified'], 0)
        self.assertTrue(self.deleted(mock_passthrough))

    def test_not_deleted_when_notify_append_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('NO', [b'Over quota'])
        stats = self.run_process(mock_passthrough, mock_notify)

        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_not_deleted_when_full_fetch_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID, fetch_full=('NO', [None]))
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        stats = self.run_process(mock_passthrough, mock_notify)

        mock_notify.execute.assert_not_called()
        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_missing_message_id_not_deleted_when_copy_fails(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITHOUT_ID, noop=False)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('NO', [b'Over quota'])
        stats = self.run_process(mock_passthrough, mock_notify)

        self.assertFalse(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 0)

    def test_missing_message_id_deleted_once_copied(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITHOUT_ID, noop=False)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        stats = self.run_process(mock_passthrough, mock_notify)

        self.assertTrue(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 1)

    def test_gmail_inbox_copy_failure_still_deletes(self):
        from unittest.mock import MagicMock
        from email_fixer import Config

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        mock_gmail = MagicMock()
        mock_gmail.execute.return_value = ('NO', [b'Over quota'])
        config = Config(300, True, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        stats = {'checked': 0, 'notified': 0}

        from unittest.mock import patch
        from email_fixer import process_single_message
        with patch('email_fixer.check_gmail_for_message', return_value=False), \
             patch('email_fixer.time.sleep'):
            process_single_message(mock_passthrough, mock_gmail, mock_notify, b'1', stats, config)

        self.assertTrue(self.deleted(mock_passthrough))
        self.assertEqual(stats['notified'], 1)

    def test_passthrough_addresses_messages_by_uid(self):
        from unittest.mock import MagicMock

        mock_passthrough = self.make_passthrough(self.HEADER_WITH_ID)
        mock_notify = MagicMock()
        mock_notify.execute.return_value = ('OK', [b''])
        self.run_process(mock_passthrough, mock_notify)

        calls = [call.args for call in mock_passthrough.execute.call_args_list]
        self.assertIn(('uid', 'FETCH', b'1', '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])'), calls)
        self.assertIn(('uid', 'FETCH', b'1', '(RFC822)'), calls)
        self.assertIn(('uid', 'STORE', b'1', '+FLAGS', '\\Deleted'), calls)
        # Nothing may address a message by sequence number, which shifts on expunge.
        self.assertEqual([], [c for c in calls if c[0] in ('fetch', 'store', 'search')])

    def test_process_passthrough_emails_searches_by_uid(self):
        from unittest.mock import MagicMock, patch
        from email_fixer import process_passthrough_emails, Config

        passthrough = MagicMock()
        passthrough.is_connected.return_value = True
        passthrough.execute.return_value = ('OK', [b''])
        config = Config(300, False, "imap.test", 993, "u", "p", "", 993, "", "", "", 993, "", "")

        with patch('email_fixer.IMAPClient') as mock_cls:
            mock_cls.return_value.__enter__.return_value = passthrough
            process_passthrough_emails({'checked': 0, 'notified': 0}, config)

        passthrough.execute.assert_called_once_with('uid', 'SEARCH', None, 'ALL')

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
