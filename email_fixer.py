import imaplib
import email
import os
import time
import logging
import signal
import datetime
import re
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from threading import Event
from typing import Optional, List, Dict, Any, Generator
from dotenv import load_dotenv

shutdown_event = Event()

def handle_shutdown(signum: int, frame: Any) -> None:
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
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

@contextmanager
def imap_connection(
    server: str, port: int, user: str, password: str, folder: Optional[str] = 'INBOX', readonly: bool = False
) -> Generator[Optional[imaplib.IMAP4_SSL], None, None]:
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(user, password)
        if folder:
            mail.select(folder, readonly=readonly)
        yield mail
    except Exception as e:
        logger.error(f"Failed to connect to IMAP {user}@{server}: {e}")
        yield None
    finally:
        if mail:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass

def extract_message_id(raw_email: bytes) -> str:
    msg = email.message_from_bytes(raw_email)
    msg_id = msg.get('Message-ID', '')
    if msg_id:
        msg_id = msg_id.replace('\r', '').replace('\n', '').strip()
        if len(msg_id) > 255:
            msg_id = msg_id[:255]
    return msg_id

def get_folders_to_check(mail: imaplib.IMAP4_SSL) -> List[str]:
    all_mail = '"[Gmail]/All Mail"'
    spam = '"[Gmail]/Spam"'
    trash = '"[Gmail]/Trash"'
    
    try:
        status, folders = mail.list()
        if status == 'OK':
            for folder_bytes in folders:
                folder_str = folder_bytes.decode('utf-8', errors='ignore')
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
        
    return [all_mail, spam, trash]

def check_gmail_for_message(gmail: Optional[imaplib.IMAP4_SSL], message_id: str) -> bool:
    if not message_id or not gmail:
        return False
        
    try:
        folders_to_check = get_folders_to_check(gmail)
        
        for target_folder in folders_to_check:
            status, response = gmail.select(target_folder, readonly=True)
            
            if status != 'OK':
                logger.debug(f"Could not select folder ({target_folder}). Response: {response}.")
                if target_folder == folders_to_check[0]:
                    logger.warning(f"Could not select All Mail folder ({target_folder}). Falling back to INBOX.")
                    status, response = gmail.select('INBOX', readonly=True)
                    if status != 'OK':
                        continue
                else:
                    continue
            
            status, response = gmail.search(None, 'HEADER', 'Message-ID', message_id)
            if status == 'OK':
                msg_ids = response[0].split()
                if len(msg_ids) > 0:
                    return True
                    
            status, response = gmail.search(None, 'X-GM-RAW', f'rfc822msgid:{message_id}')
            if status == 'OK':
                msg_ids = response[0].split()
                if len(msg_ids) > 0:
                    return True
    except Exception as e:
        logger.error(f"Error searching GMail for {message_id}: {e}")
            
    return False

def copy_to_imap(mail_client: Optional[imaplib.IMAP4_SSL], raw_email: bytes, folder: str = 'INBOX') -> bool:
    if not mail_client:
        return False
    try:
        mail_client.append(folder, None, imaplib.Time2Internaldate(time.time()), raw_email)
        return True
    except Exception as e:
        logger.error(f"Error copying message: {e}")
        return False

def process_single_message(
    passthrough: imaplib.IMAP4_SSL, 
    gmail: Optional[imaplib.IMAP4_SSL], 
    notify_mail: Optional[imaplib.IMAP4_SSL], 
    num: bytes, 
    stats: Dict[str, int], 
    config: Config
) -> None:
    status, data = passthrough.fetch(num, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
    if status != 'OK':
        logger.error(f"Failed to fetch header for message {num.decode('utf-8')}")
        return
        
    header_data = data[0][1]
    message_id = extract_message_id(header_data)
    
    logger.debug(f"Processing message ID: {message_id}")
    print('.', end='', flush=True)
    stats['checked'] += 1
    
    if not message_id:
        logger.warning(f"\nCould not extract Message-ID from message {num.decode('utf-8')}. Copying to Notify to be safe.")
        print("\nMessage (no ID) not found in GMail. Sent to notifier.", flush=True)
        stats['notified'] += 1
        
        full_status, full_data = passthrough.fetch(num, '(RFC822)')
        if full_status == 'OK':
            copy_to_imap(notify_mail, full_data[0][1])
            if config.copy_to_gmail_inbox:
                copy_to_imap(gmail, full_data[0][1])
            
        passthrough.store(num, '+FLAGS', '\\Deleted')
        return
    
    time.sleep(5)
    
    try:
        passthrough.noop()
    except Exception:
        pass

    found_in_gmail = check_gmail_for_message(gmail, message_id)
    
    if found_in_gmail:
        logger.debug(f"Message {message_id} found in GMail.")
    else:
        logger.debug(f"Message {message_id} NOT found in GMail. Copying to Notify...")
        print(f"\nMessage {message_id} not found in GMail. Sent to notifier.", flush=True)
        stats['notified'] += 1
        
        full_status, full_data = passthrough.fetch(num, '(RFC822)')
        if full_status == 'OK':
            copy_to_imap(notify_mail, full_data[0][1])
            if config.copy_to_gmail_inbox:
                logger.debug(f"Also copying message {message_id} to GMail Inbox...")
                copy_to_imap(gmail, full_data[0][1])
        
    passthrough.store(num, '+FLAGS', '\\Deleted')
    logger.debug(f"Marked message {num.decode('utf-8')} for deletion in PassThrough.")


def process_passthrough_emails(stats: Dict[str, int], config: Config) -> None:
    with ExitStack() as stack:
        passthrough = stack.enter_context(
            imap_connection(config.passthrough_server, config.passthrough_port, config.passthrough_user, config.passthrough_pass)
        )
        
        if not passthrough:
            return
            
        gmail = stack.enter_context(
            imap_connection(config.gmail_server, config.gmail_port, config.gmail_user, config.gmail_pass, folder=None)
        )
        
        notify_mail = stack.enter_context(
            imap_connection(config.notify_server, config.notify_port, config.notify_user, config.notify_pass)
        )
        
        try:
            status, response = passthrough.search(None, 'ALL')
            if status != 'OK':
                logger.error("Failed to search PassThrough INBOX")
                return
                
            msg_nums = response[0].split()
            if not msg_nums:
                return
                
            logger.debug(f"Found {len(msg_nums)} messages in PassThrough")
            
            for num in msg_nums:
                process_single_message(passthrough, gmail, notify_mail, num, stats, config)
                
            passthrough.expunge()
            
        except Exception as e:
            logger.error(f"Error processing PassThrough: {e}")

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
            process_passthrough_emails(stats, config)
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            
        shutdown_event.wait(config.check_interval)

if __name__ == "__main__":
    main()
