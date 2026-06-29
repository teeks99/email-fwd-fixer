import imaplib
import email
import os
import time
import logging
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def connect_imap(server, port, user, password, readonly=False):
    try:
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(user, password)
        mail.select('INBOX', readonly=readonly)
        return mail
    except Exception as e:
        logger.error(f"Failed to connect to IMAP {user}@{server}: {e}")
        return None

def extract_message_id(raw_email):
    msg = email.message_from_bytes(raw_email)
    return msg.get('Message-ID', '').strip()

def check_gmail_for_message(message_id):
    if not message_id:
        return False
        
    gmail = connect_imap(
        os.getenv("GMAIL_IMAP_SERVER"),
        os.getenv("GMAIL_IMAP_PORT", 993),
        os.getenv("GMAIL_IMAP_USER"),
        os.getenv("GMAIL_IMAP_PASS"),
        readonly=True
    )
    
    if not gmail:
        return False
        
    try:
        # Search by Message-ID
        search_query = f'(HEADER Message-ID "{message_id}")'
        status, response = gmail.search(None, search_query)
        if status == 'OK':
            msg_ids = response[0].split()
            return len(msg_ids) > 0
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

def process_passthrough_emails():
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
            
        logger.info(f"Found {len(msg_nums)} messages in PassThrough")
        
        for num in msg_nums:
            # Fetch the raw message
            status, data = passthrough.fetch(num, '(RFC822)')
            if status != 'OK':
                logger.error(f"Failed to fetch message {num}")
                continue
                
            raw_email = data[0][1]
            message_id = extract_message_id(raw_email)
            
            logger.info(f"Processing message ID: {message_id}")
            
            found_in_gmail = check_gmail_for_message(message_id)
            
            if found_in_gmail:
                logger.info(f"Message {message_id} found in GMail.")
            else:
                logger.info(f"Message {message_id} NOT found in GMail. Copying to Notify...")
                copy_to_notify(raw_email)
                
            # Delete from PassThrough
            passthrough.store(num, '+FLAGS', '\\Deleted')
            logger.info(f"Marked message {num} for deletion in PassThrough.")
            
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
    
    check_interval = int(os.getenv("CHECK_INTERVAL_SECONDS", 300))
    
    logger.info("Starting Email Forward Fixer Service...")
    
    while True:
        try:
            process_passthrough_emails()
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            
        time.sleep(check_interval)

if __name__ == "__main__":
    main()
