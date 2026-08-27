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

    def test_process_single_message_not_found_timestamp(self):
        from unittest.mock import MagicMock, patch
        import io
        import re
        from email_fixer import process_single_message, Config

        mock_passthrough = MagicMock()
        mock_passthrough.execute.side_effect = [
            ('OK', [(None, b"Message-ID: <test1234@example.com>\r\nFrom: sender@test.com\r\nSubject: Hello\r\n")]),
            ('OK', [b'']),
            ('OK', [(None, b"full raw email content")]),
            ('OK', [b''])
        ]
        mock_gmail = MagicMock()
        mock_notify = MagicMock()
        config = Config(300, False, "", 993, "", "", "", 993, "", "", "", 993, "", "")
        stats = {'checked': 0, 'notified': 0}

        with patch('email_fixer.check_gmail_for_message', return_value=False), \
             patch('email_fixer.time.sleep'), \
             patch('sys.stdout', new=io.StringIO()) as fake_out:
            process_single_message(mock_passthrough, mock_gmail, mock_notify, b'1', stats, config)
            output = fake_out.getvalue()
            # Verify timestamp matches YYYY-MM-DD HH:MM:SS format before message
            pattern = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - Message <test1234@example.com> not found in GMail'
            self.assertRegex(output, pattern)

if __name__ == "__main__":
    unittest.main()
