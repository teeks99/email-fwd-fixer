import imaplib
import email
from email.header import decode_header, make_header
import os
import time
import logging
import signal
import datetime
import re
from dataclasses import dataclass
from threading import Event
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

shutdown_event = Event()

# How long to let a forward settle before asking GMail whether it arrived. The
# forward and the GMail delivery race, so looking too early reports mail that is
# merely still in flight as missing.
GMAIL_SETTLE_SECONDS = 5

def handle_shutdown(signum: int, frame: Any) -> None:
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

@dataclass
class Config:
    check_interval: int
    copy_to_gmail_inbox: bool
    passthrough_server: str
    passthrough_port: int
    passthrough_user: str
    passthrough_pass: str
    gmail_server: str
    gmail_port: int
    gmail_user: str
    gmail_pass: str
    notify_server: str
    notify_port: int
    notify_user: str
    notify_pass: str

def load_config() -> Config:
    load_dotenv()
    return Config(
        check_interval=int(os.getenv("CHECK_INTERVAL_SECONDS", 300)),
        copy_to_gmail_inbox=os.getenv("COPY_TO_GMAIL_INBOX", "false").lower() == "true",
        passthrough_server=os.getenv("PASSTHROUGH_IMAP_SERVER", ""),
        passthrough_port=int(os.getenv("PASSTHROUGH_IMAP_PORT", 993)),
        passthrough_user=os.getenv("PASSTHROUGH_IMAP_USER", ""),
        passthrough_pass=os.getenv("PASSTHROUGH_IMAP_PASS", ""),
        gmail_server=os.getenv("GMAIL_IMAP_SERVER", ""),
        gmail_port=int(os.getenv("GMAIL_IMAP_PORT", 993)),
        gmail_user=os.getenv("GMAIL_IMAP_USER", ""),
        gmail_pass=os.getenv("GMAIL_IMAP_PASS", ""),
        notify_server=os.getenv("NOTIFY_IMAP_SERVER", ""),
        notify_port=int(os.getenv("NOTIFY_IMAP_PORT", 993)),
        notify_user=os.getenv("NOTIFY_IMAP_USER", ""),
        notify_pass=os.getenv("NOTIFY_IMAP_PASS", "")
    )

class IMAPClient:
    """Robust IMAP client wrapper that manages connections, folder selection,
    and automatic reconnection if session state becomes invalid or connection drops."""

    def __init__(self, server: str, port: int, user: str, password: str, folder: Optional[str] = 'INBOX', readonly: bool = False):
        self.server = server
        self.port = port
        self.user = user
        self.password = password
        self.default_folder = folder
        self.readonly = readonly
        self.current_folder: Optional[str] = None
        self.current_readonly: bool = readonly
        self.mail: Optional[imaplib.IMAP4_SSL] = None
        self._folders_to_check: Optional[List[str]] = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self) -> bool:
        # Tear the old session down without expunging. IMAP CLOSE purges every
        # message already flagged \Deleted from a writable mailbox, and a
        # reconnect can land part way through a run of deletions.
        self.close(expunge=False)
        if not self.server or not self.user:
            return False
        try:
            self.mail = imaplib.IMAP4_SSL(self.server, self.port)
            self.mail.login(self.user, self.password)
            self.current_folder = None
            if self.default_folder:
                return self.select_folder(self.default_folder, readonly=self.readonly)
            return True
        except Exception as e:
            logger.error(f"Failed to connect to IMAP {self.user}@{self.server}: {e}")
            self.mail = None
            return False

    def is_connected(self) -> bool:
        if not self.mail:
            return False
        try:
            state = getattr(self.mail, 'state', None)
            return state in ('AUTH', 'SELECTED')
        except Exception:
            return False

    def select_folder(self, folder: str, readonly: bool = False) -> bool:
        if not self.is_connected():
            if not self.connect():
                return False
        try:
            status, data = self.mail.select(folder, readonly=readonly)
            if status == 'OK':
                self.current_folder = folder
                self.current_readonly = readonly
                return True
            else:
                logger.debug(f"Could not select folder '{folder}' on {self.user}@{self.server}: {data}")
                return False
        except Exception as e:
            logger.warning(f"Error selecting folder '{folder}' on {self.user}@{self.server}: {e}. Reconnecting...")
            if self.connect():
                try:
                    status, data = self.mail.select(folder, readonly=readonly)
                    if status == 'OK':
                        self.current_folder = folder
                        self.current_readonly = readonly
                        return True
                except Exception:
                    pass
            return False

    def select(self, folder: str, readonly: bool = False):
        """Compatibility wrapper for select() returning (status, data)."""
        success = self.select_folder(folder, readonly=readonly)
        if success:
            return 'OK', [b'']
        else:
            return 'NO', [b'Failed to select folder']

    def execute(self, func_name: str, *args, **kwargs) -> Any:
        """Execute an imaplib method with automatic reconnect on connection or state errors.

        The reconnect drops the old session without expunging, so a command that
        fails mid-pass never destroys messages flagged earlier in that pass."""
        if not self.is_connected():
            if not self.connect():
                raise Exception(f"Not connected to IMAP server {self.user}@{self.server}")

        try:
            method = getattr(self.mail, func_name)
            return method(*args, **kwargs)
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError, Exception) as e:
            logger.warning(f"IMAP command '{func_name}' failed on {self.user}@{self.server}: {e}. Reconnecting...")
            if self.connect():
                if self.current_folder:
                    self.select_folder(self.current_folder, readonly=self.current_readonly)
                method = getattr(self.mail, func_name)
                return method(*args, **kwargs)
            else:
                raise

    def close(self, expunge: bool = True) -> None:
        """Tear down the connection.

        IMAP CLOSE permanently removes every \\Deleted message from a writable
        mailbox, so callers that are only swapping out a stale socket must pass
        expunge=False. LOGOUT on its own never expunges."""
        if self.mail:
            if expunge:
                try:
                    if getattr(self.mail, 'state', None) == 'SELECTED':
                        self.mail.close()
                except Exception:
                    pass
            try:
                self.mail.logout()
            except Exception:
                pass
            self.mail = None
            self.current_folder = None

def decode_header_value(header_val: Optional[str]) -> str:
    if not header_val:
        return ""
    try:
        decoded_str = str(make_header(decode_header(header_val)))
    except Exception:
        decoded_str = str(header_val)
    return re.sub(r'\s+', ' ', decoded_str).strip()

def extract_message_details(raw_email: bytes) -> Dict[str, str]:
    msg = email.message_from_bytes(raw_email)
    msg_id = msg.get('Message-ID', '')
    if msg_id:
        msg_id = msg_id.replace('\r', '').replace('\n', '').strip()
        if len(msg_id) > 255:
            msg_id = msg_id[:255]
            
    sender = decode_header_value(msg.get('From', ''))
    subject = decode_header_value(msg.get('Subject', ''))
    
    return {
        'message_id': msg_id,
        'sender': sender or 'Unknown Sender',
        'subject': subject or 'No Subject'
    }

def extract_message_id(raw_email: bytes) -> str:
    return extract_message_details(raw_email)['message_id']
@dataclass
class Message:
    """One PassThrough message, identified by a UID rather than a sequence number.

    Sequence numbers are positional and shift down whenever the mailbox is
    expunged; a UID stays bound to this message for the life of the mailbox."""
    uid: bytes
    message_id: str
    sender: str
    subject: str

    @property
    def uid_str(self) -> str:
        return self.uid.decode('utf-8', errors='ignore')

    def __str__(self) -> str:
        return f"UID {self.uid_str} (From: {self.sender}, Subject: {self.subject})"

class ForwardFixer:
    """Drains the PassThrough mailbox, losing nothing on the way.

    PassThrough receives forwarded mail that GMail sometimes drops silently. For
    each message the fixer answers one question -- is this message safe to remove
    from PassThrough? -- and there is exactly one answer that permits removal:

      * GMail is confirmed to already hold it, or
      * Notify is confirmed to hold a copy.

    Anything else leaves the message where it is, to be retried next pass.

    Three mailboxes are involved and only one of them is ever deleted from:

      PassThrough  INBOX, writable  -- searched, fetched, flagged, expunged
      GMail        readonly         -- searched; appended to only if configured
      Notify       INBOX            -- appended to; never flagged or deleted
    """

    def __init__(self, config: Config, stats: Dict[str, int]):
        self.config = config
        self.stats = stats
        self.passthrough: Optional[IMAPClient] = None
        self.gmail: Optional[IMAPClient] = None
        self.notify: Optional[IMAPClient] = None
        self._gmail_folder_cache: Optional[List[str]] = None

    # -- the policy ---------------------------------------------------------

    def run_once(self) -> None:
        """One poll cycle over the PassThrough INBOX.

        Every message is decided on its own; the single EXPUNGE at the end is the
        only moment at which anything is permanently removed."""
        try:
            if not self._connect_passthrough():
                return

            uids = self._waiting_uids()
            if not uids:
                return

            self._connect_gmail_and_notify()
            for uid in uids:
                try:
                    self.handle_message(uid)
                except Exception as e:
                    logger.error(f"Error processing message UID {uid.decode('utf-8', errors='ignore')}: {e}")
            print(flush=True)
            self._expunge_passthrough()
        except Exception as e:
            logger.error(f"Error processing PassThrough: {e}")
        finally:
            self._close_all()

    def handle_message(self, uid: bytes) -> None:
        """Decide the fate of a single PassThrough message.

        The delete at the bottom is only reachable down a path that has already
        secured the message somewhere else. Every other path returns early and
        leaves the original in PassThrough."""
        message = self._read_headers(uid)
        if message is None:
            return

        self.stats['checked'] += 1
        print('.', end='', flush=True)

        if message.message_id and self._gmail_has(message):
            logger.debug(f"Message {message.message_id} found in GMail.")
        else:
            self._log_rescue_reason(message)

            raw_email = self._fetch_raw(message)
            if raw_email is None:
                return  # unreadable -- leave the original in PassThrough

            if not self._copy_to_notify(message, raw_email):
                return  # secured nowhere -- leave the original in PassThrough
            self.stats['notified'] += 1

            if self.config.copy_to_gmail_inbox:
                self._copy_to_gmail_inbox(message, raw_email)

        self._mark_deleted(message)

    # -- PassThrough --------------------------------------------------------

    def _waiting_uids(self) -> List[bytes]:
        """UIDs of everything sitting in the PassThrough INBOX."""
        status, response = self.passthrough.execute('uid', 'SEARCH', None, 'ALL')
        if status != 'OK':
            logger.error("Failed to search PassThrough INBOX")
            return []
        uids = (response[0] or b'').split()
        logger.debug(f"Found {len(uids)} messages in PassThrough")
        return uids

    def _read_headers(self, uid: bytes) -> Optional[Message]:
        """Peek at the headers needed to identify a message, leaving it unread."""
        status, data = self.passthrough.execute(
            'uid', 'FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])')
        if status != 'OK' or not data or not data[0]:
            logger.error(f"Failed to fetch header for message UID {uid.decode('utf-8', errors='ignore')}")
            return None
        return Message(uid=uid, **extract_message_details(data[0][1]))

    def _fetch_raw(self, message: Message) -> Optional[bytes]:
        """Pull a message's full body. One fetch serves every destination."""
        status, data = self.passthrough.execute('uid', 'FETCH', message.uid, '(RFC822)')
        if status != 'OK' or not data or not data[0]:
            logger.error(f"Failed to fetch the body of message {message} from PassThrough. Leaving it to retry.")
            return None
        return data[0][1]

    def _mark_deleted(self, message: Message) -> None:
        """Flag the PassThrough original. The flag is persistent server-side, so
        the message survives until run_once's closing EXPUNGE."""
        self.passthrough.execute('uid', 'STORE', message.uid, '+FLAGS', '\\Deleted')
        logger.debug(f"Marked message UID {message.uid_str} for deletion in PassThrough.")

    def _expunge_passthrough(self) -> None:
        try:
            self.passthrough.execute('expunge')
        except Exception as e:
            logger.error(f"Error expunging PassThrough INBOX: {e}")

    # -- GMail --------------------------------------------------------------

    def _gmail_has(self, message: Message) -> bool:
        """Has GMail already received this message?

        Waits first. The GMail delivery and the PassThrough forward race each
        other, so an immediate lookup reports mail that is merely still in flight
        as missing. The NOOP holds the PassThrough session open across the wait."""
        time.sleep(GMAIL_SETTLE_SECONDS)
        try:
            self.passthrough.execute('noop')
        except Exception:
            pass

        if not self.gmail or not self.gmail.is_connected():
            return False

        try:
            for folder in self._gmail_folders():
                if not self._select_gmail_folder(folder):
                    continue
                for criteria in (('HEADER', 'Message-ID', message.message_id),
                                 ('X-GM-RAW', f'rfc822msgid:{message.message_id}')):
                    status, response = self.gmail.execute('search', None, *criteria)
                    if status == 'OK' and response and response[0] and response[0].split():
                        return True
        except Exception as e:
            logger.error(f"Error searching GMail for {message.message_id}: {e}")
        return False

    def _select_gmail_folder(self, folder: str) -> bool:
        """Select a GMail folder read-only, falling back to INBOX for All Mail."""
        status, response = self.gmail.select(folder, readonly=True)
        if status == 'OK':
            return True

        logger.debug(f"Could not select folder ({folder}). Response: {response}.")
        if folder != self._gmail_folders()[0]:
            return False

        logger.warning(f"Could not select All Mail folder ({folder}). Falling back to INBOX.")
        status, _ = self.gmail.select('INBOX', readonly=True)
        return status == 'OK'

    def _gmail_folders(self) -> List[str]:
        """All Mail, Spam and Trash, discovered from the server's LIST flags.

        A forwarded message that GMail filed as spam or deleted still counts as
        delivered, so all three are searched."""
        if self._gmail_folder_cache:
            return self._gmail_folder_cache

        all_mail = '"[Gmail]/All Mail"'
        spam = '"[Gmail]/Spam"'
        trash = '"[Gmail]/Trash"'

        try:
            status, folders = self.gmail.execute('list')
            if status == 'OK' and folders:
                for folder_bytes in folders:
                    if not folder_bytes:
                        continue
                    folder_str = folder_bytes.decode('utf-8', errors='ignore') if isinstance(folder_bytes, bytes) else str(folder_bytes)
                    folder_lower = folder_str.lower()

                    if '\\all' in folder_lower or '\\junk' in folder_lower or '\\spam' in folder_lower or '\\trash' in folder_lower:
                        match = re.search(r'"([^"]+)"$', folder_str)
                        if match:
                            folder_name = f'"{match.group(1)}"'
                        else:
                            folder_name = folder_str.split()[-1]

                        if '\\all' in folder_lower:
                            all_mail = folder_name
                        elif '\\junk' in folder_lower or '\\spam' in folder_lower:
                            spam = folder_name
                        elif '\\trash' in folder_lower:
                            trash = folder_name
        except Exception as e:
            logger.error(f"Error querying folder list: {e}")

        self._gmail_folder_cache = [all_mail, spam, trash]
        return self._gmail_folder_cache

    # -- Notify -------------------------------------------------------------

    def _log_rescue_reason(self, message: Message) -> None:
        """Say why this message is being copied out, before anything is copied."""
        print(flush=True)  # close off the run of progress dots
        if message.message_id:
            logger.info(f"Message {message.message_id} NOT found in GMail (From: {message.sender}, Subject: {message.subject}). Copying to Notify...")
        else:
            logger.warning(f"Could not extract Message-ID from message {message}. Copying to Notify to be safe.")

    def _copy_to_notify(self, message: Message, raw_email: bytes) -> bool:
        """Secure a copy in Notify. This is the copy that counts.

        Returns True only once the APPEND is confirmed -- that confirmation is
        the single thing that makes deleting the PassThrough original safe."""
        if not self._append(self.notify, raw_email):
            logger.error(f"Failed to copy message {message} to Notify. Leaving it in PassThrough to retry.")
            return False
        return True

    def _copy_to_gmail_inbox(self, message: Message, raw_email: bytes) -> None:
        """Also drop a copy in the GMail Inbox, for COPY_TO_GMAIL_INBOX setups.

        Best effort, and deliberately returns nothing: the message is already
        safe in Notify, so a failure here is worth a warning but must not hold up
        removing the PassThrough original."""
        logger.debug(f"Also copying message {message} to GMail Inbox...")
        if not self._append(self.gmail, raw_email):
            logger.warning(f"Failed to copy message {message} to the GMail Inbox (it is backed up in Notify).")

    @staticmethod
    def _append(client: Optional[IMAPClient], raw_email: bytes, folder: str = 'INBOX') -> bool:
        if not client or not client.is_connected():
            return False
        try:
            status, _ = client.execute('append', folder, None, imaplib.Time2Internaldate(time.time()), raw_email)
            return status == 'OK'
        except Exception as e:
            logger.error(f"Error copying message: {e}")
            return False

    # -- connections --------------------------------------------------------

    def _connect_passthrough(self) -> bool:
        """Open the PassThrough INBOX -- the only mailbox this service deletes from."""
        cfg = self.config
        self.passthrough = IMAPClient(cfg.passthrough_server, cfg.passthrough_port,
                                      cfg.passthrough_user, cfg.passthrough_pass, folder='INBOX')
        self.passthrough.connect()
        return self.passthrough.is_connected()

    def _connect_gmail_and_notify(self) -> None:
        """Open the lookup and backup mailboxes.

        Held back until PassThrough reports work, so an idle poll costs one login
        rather than three. GMail opens with no folder because each lookup selects
        All Mail, Spam or Trash for itself."""
        cfg = self.config
        self.gmail = IMAPClient(cfg.gmail_server, cfg.gmail_port,
                                cfg.gmail_user, cfg.gmail_pass, folder=None)
        self.gmail.connect()

        self.notify = IMAPClient(cfg.notify_server, cfg.notify_port,
                                 cfg.notify_user, cfg.notify_pass, folder='INBOX')
        self.notify.connect()

    def _close_all(self) -> None:
        """Close whatever got opened. Safe on a client that never connected, and
        safe to reach with GMail and Notify still unopened."""
        for client in (self.notify, self.gmail, self.passthrough):
            if client:
                client.close()
        self.passthrough = self.gmail = self.notify = None

def main() -> None:
    config = load_config()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    logger.info("Starting Email Forward Fixer Service")

    current_date = datetime.date.today()
    stats = {'checked': 0, 'notified': 0}

    while not shutdown_event.is_set():
        today = datetime.date.today()
        if today > current_date:
            print()
            logger.info(f"Daily Summary: {stats['checked']} messages checked, {stats['notified']} notified.")
            stats = {'checked': 0, 'notified': 0}
            current_date = today

        try:
            ForwardFixer(config, stats).run_once()
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")

        shutdown_event.wait(config.check_interval)

if __name__ == "__main__":
    main()
