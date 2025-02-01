# email_utils.py

import aiosmtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.audio import MIMEAudio
from typing import List, Optional
from pathlib import Path

from helper import get_mime_type


async def send_email(
    to: str,
    subject: str,
    body_html: str,
    smtp_server: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    from_email: str = None,
    use_ssl: bool = False,
    attachments: Optional[List[str]] = None,
):
    """
    Sends an HTML email to the specified 'to' address using the provided 
    SMTP settings. Uses aiosmtplib for asynchronous sending.
    
    """

    # 1. Construct the email message
    # Use a "mixed" container for both body and attachments
    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = from_email or smtp_username
    message["To"] = to

    # 2. Attach the HTML part as a sub-part of type "alternative"
    alternative_part = MIMEMultipart("alternative")
    html_part = MIMEText(body_html, "html")
    alternative_part.attach(html_part)

    # Attach the "alternative" part to the main "mixed" message
    message.attach(alternative_part)

    # 3. Attach any files
    if attachments:
        for file_path in attachments:
            try:
                # Detect the MIME type
                mime_type = get_mime_type(file_path)
                main_type, _, sub_type = mime_type.partition("/")
                if not sub_type:  
                    # If partition failed or mime_type was missing '/', fallback
                    main_type, sub_type = "application", "octet-stream"

                with open(file_path, "rb") as f:
                    file_data = f.read()

                if main_type == "image":
                    # Example: image/jpeg, image/png, etc.
                    part = MIMEImage(file_data, _subtype=sub_type)
                elif main_type == "audio":
                    # Example: audio/mpeg, audio/ogg, etc.
                    part = MIMEAudio(file_data, _subtype=sub_type)
                elif main_type == "text":
                    # If it's text/*, we can use MIMEText. 
                    # But watch out for encodings (e.g. plain text vs. HTML).
                    part = MIMEText(file_data.decode("utf-8", errors="replace"), _subtype=sub_type)
                else:
                    # Fallback to generic base
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(file_data)
                    encoders.encode_base64(part)

                filename = Path(file_path).name
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"'
                )
                message.attach(part)

            except Exception as e:
                # Handle the error for a single attachment, e.g., log or re-raise
                print(f"Error attaching file {file_path}: {e}")
                # Optionally raise or continue, as desired.

    # 4. Connect and send via aiosmtplib
    if use_ssl:
        # Implicit TLS on port 465
        await aiosmtplib.send(
            message,
            hostname=smtp_server,
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            use_tls=True,
        )
    else:
        # STARTTLS on port 587
        await aiosmtplib.send(
            message,
            hostname=smtp_server,
            port=smtp_port,
            username=smtp_username,
            password=smtp_password,
            start_tls=True,
        )


async def send_batch_email(automation, to_address, batch_name, attachment_paths=None):
    """
    Demonstrates how to call send_email for a given automation config and recipient,
    optionally with a list of attachment file paths.
    """
    subject = f"Automation Batch: {batch_name}"
    body_html = f"""
    <html>
      <body>
        <h2>Hello!</h2>
        <p>This is an automated email for batch: {batch_name}</p>
      </body>
    </html>
    """

    # Decide use_ssl based on port or your own logic
    use_ssl = True if automation.port == 465 else False

    await send_email(
        to=to_address,
        subject=subject,
        body_html=body_html,
        smtp_server=automation.server,
        smtp_port=automation.port,
        smtp_username=automation.username,
        smtp_password=automation.password,
        from_email=automation.email,
        use_ssl=use_ssl,
        attachments=attachment_paths or [],
    )
