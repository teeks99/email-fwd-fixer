import imaplib
import email
import os
import time
import logging
import signal
import sys
import datetime
from threading import Event
from dotenv import load_dotenv

shutdown_event = Event()

def handle_shutdown(signum, frame):
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def connect_imap(server, port, user, password, folder='INBOX', readonly=False):
    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(user, password)
        if folder:
            mail.select(folder, readonly=readonly)
        return mail
    except Exception as e:
        logger.error(f"Failed to connect to IMAP {user}@{server}: {e}")
        return None

def extract_message_id(raw_email):
    msg = email.message_from_bytes(raw_email)
    msg_id = msg.get('Message-ID', '')
    # Sanitize to prevent IMAP/Log injection
    if msg_id:
        msg_id = msg_id.replace('\r', '').replace('\n', '').strip()
        # Cap length to prevent excessively long IDs
        if len(msg_id) > 255:
            msg_id = msg_id[:255]
    return msg_id

def get_folders_to_check(mail):
    all_mail = '"[Gmail]/All Mail"'
    spam = '"[Gmail]/Spam"'
    trash = '"[Gmail]/Trash"'
    
    try:
        status, folders = mail.list()
        if status == 'OK':
            import re
            for folder_bytes in folders:
                folder_str = folder_bytes.decode('utf-8', errors='ignore')
                folder_lower = folder_str.lower()
                
                if '\\all' in folder_lower or '\\junk' in folder_lower or '\\spam' in folder_lower or '\\trash' in folder_lower:
                    # The folder name is at the end of the LIST response
                    # Try to extract it if it's quoted
                    match = re.search(r'"([^"]+)"$', folder_str)
                    if match:
                        folder_name = f'"{match.group(1)}"'
                    else:
                        # Fallback if unquoted
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

def check_gmail_for_message(message_id):
    if not message_id:
        return False
        
    gmail = connect_imap(
        os.getenv("GMAIL_IMAP_SERVER"),
        os.getenv("GMAIL_IMAP_PORT", 993),
        os.getenv("GMAIL_IMAP_USER"),
        os.getenv("GMAIL_IMAP_PASS"),
        folder=None
    )
    
    if not gmail:
        return False
        
    try:
        # Dynamically find the special folders regardless of language
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
            
            # Search by Message-ID safely using imaplib's native argument quoting
            status, response = gmail.search(None, 'HEADER', 'Message-ID', message_id)
            if status == 'OK':
                msg_ids = response[0].split()
                if len(msg_ids) > 0:
                    return True
                    
            # Fallback to Gmail's native search engine (X-GM-RAW) just in case
            status, response = gmail.search(None, 'X-GM-RAW', f'rfc822msgid:{message_id}')
            if status == 'OK':
                msg_ids = response[0].split()
                if len(msg_ids) > 0:
                    return True
    except Exception as e:
        logger.error(f"Error searching GMail for {message_id}: {e}")
    finally:
        try:
            gmail.close()
            gmail.logout()
        except:
            pass
            
    return False

def copy_to_notify(raw_email):
    notify_mail = connect_imap(
        os.getenv("NOTIFY_IMAP_SERVER"),
        os.getenv("NOTIFY_IMAP_PORT", 993),
        os.getenv("NOTIFY_IMAP_USER"),
        os.getenv("NOTIFY_IMAP_PASS")
    )
    
    if not notify_mail:
        return False
        
    try:
        notify_mail.append('INBOX', None, imaplib.Time2Internaldate(time.time()), raw_email)
        return True
    except Exception as e:
        logger.error(f"Error copying message to Notify: {e}")
        return False
    finally:
        try:
            notify_mail.close()
            notify_mail.logout()
        except:
            pass

def process_passthrough_emails(stats):
    passthrough = connect_imap(
        os.getenv("PASSTHROUGH_IMAP_SERVER"),
        os.getenv("PASSTHROUGH_IMAP_PORT", 993),
        os.getenv("PASSTHROUGH_IMAP_USER"),
        os.getenv("PASSTHROUGH_IMAP_PASS")
    )
    
    if not passthrough:
        return
        
    try:
        # Search for all messages
        status, response = passthrough.search(None, 'ALL')
        if status != 'OK':
            logger.error("Failed to search PassThrough INBOX")
            return
            
        msg_nums = response[0].split()
        if not msg_nums:
            return
            
        logger.debug(f"Found {len(msg_nums)} messages in PassThrough")
        
        for num in msg_nums:
            # Fetch only the headers to extract Message-ID, preventing memory exhaustion (DoS) from large emails
            status, data = passthrough.fetch(num, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if status != 'OK':
                logger.error(f"Failed to fetch header for message {num}")
                continue
                
            header_data = data[0][1]
            message_id = extract_message_id(header_data)
            
            logger.debug(f"Processing message ID: {message_id}")
            print('.', end='', flush=True)
            stats['checked'] += 1
            
            if not message_id:
                logger.warning(f"\nCould not extract Message-ID from message {num}. Copying to Notify to be safe.")
                print("\nMessage (no ID) not found in GMail. Sent to notifier.", flush=True)
                stats['notified'] += 1
                
                # Fetch the full email only when we need to copy it
                full_status, full_data = passthrough.fetch(num, '(RFC822)')
                if full_status == 'OK':
                    copy_to_notify(full_data[0][1])
                    
                passthrough.store(num, '+FLAGS', '\\Deleted')
                continue
            
            # Add a small delay to give Gmail time to process and index the incoming message
            time.sleep(5)
            
            # Send a keep-alive ping to PassThrough to prevent the server from dropping the connection
            try:
                passthrough.noop()
            except Exception:
                pass

            
            found_in_gmail = check_gmail_for_message(message_id)
            
            if found_in_gmail:
                logger.debug(f"Message {message_id} found in GMail.")
            else:
                logger.debug(f"Message {message_id} NOT found in GMail. Copying to Notify...")
                print(f"\nMessage {message_id} not found in GMail. Sent to notifier.", flush=True)
                stats['notified'] += 1
                
                # Fetch the full email only because we need to copy it
                full_status, full_data = passthrough.fetch(num, '(RFC822)')
                if full_status == 'OK':
                    copy_to_notify(full_data[0][1])
                
            # Delete from PassThrough
            passthrough.store(num, '+FLAGS', '\\Deleted')
            logger.debug(f"Marked message {num} for deletion in PassThrough.")
            
        # Expunge deleted messages
        passthrough.expunge()
        
    except Exception as e:
        logger.error(f"Error processing PassThrough: {e}")
    finally:
        try:
            passthrough.close()
            passthrough.logout()
        except:
            pass

def main():
    load_dotenv()
    
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    check_interval = int(os.getenv("CHECK_INTERVAL_SECONDS", 300))
    
    logger.info("Starting Email Forward Fixer Service")
    
    current_date = datetime.date.today()
    stats = {'checked': 0, 'notified': 0}
    
    while not shutdown_event.is_set():
        today = datetime.date.today()
        if today > current_date:
            print() # Ensure the summary starts on a new line if dots were printed
            logger.info(f"Daily Summary: {stats['checked']} messages checked, {stats['notified']} notified.")
            stats = {'checked': 0, 'notified': 0}
            current_date = today
            
        try:
            process_passthrough_emails(stats)
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            
        # Wait for the interval, but wake up immediately if shutdown is requested
        shutdown_event.wait(check_interval)

if __name__ == "__main__":
    main()
