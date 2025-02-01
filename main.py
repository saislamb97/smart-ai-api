import asyncio
import csv
import io
import json
import logging
import mimetypes
import os
import aiofiles
from datetime import datetime
import uuid
import aiohttp
from fastapi import File, Form, Query, UploadFile
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
import redis
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import WebSocket, WebSocketDisconnect
from connection_manager import ConnectionManager
from email_utils import send_batch_email
from helper import decrypt_file, get_mime_type
from redis_client import redis_client, get_redis_client
# Import necessary models and functions
from enums import ChatbotStatus, CountryEnum, MessageDirection, MessageStatus, toolType
from handler import handle_chatbot_message, handle_verification
from model import (
    AutoFileModel,
    AutoFileRequestModel,
    AutomationModel,
    ChatHistoryRequest,
    ChatModel,
    ChatbotModel,
    ChatbotRequest,
    ContactModel,
    FileModel,
    SendMessageRequest,
    WhatsappMessageModel,
    WhatsappModel,
    get_db,
)
from assistant import BotData, SendWhatsAppMessage, get_media_url, gpt_completions, redis_client_sync
from model import chatbots_files

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI()

# CORS middleware to allow frontend to access the backend
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # Adjust as needed for security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize the connection manager (will be set in startup)
manager = None

# Startup and shutdown events to manage Redis connection and ConnectionManager
@app.on_event("startup")
async def startup_event():
    # Comment out Redis connection
    # await redis_client.connect()
    # redis_conn = await get_redis_client()
    global manager
    manager = ConnectionManager()
    logger.info("Connection Manager initialized without Redis.")

@app.on_event("shutdown")
async def shutdown_event():
    await redis_client.disconnect()
    logger.info("Connection Manager shutdown.")

# Root endpoint to indicate API is running
@app.get("/")
async def read_root():
    return {"status": "API is running and accessible"}

# Chatbot Interaction Endpoint
@app.post("/chatbot", operation_id="chatbotInteraction")
async def chatbot_interaction(
    request: ChatbotRequest, session: AsyncSession = Depends(get_db)
):
    try:
        # Retrieve chatbot by unique ID
        result = await session.execute(
            select(ChatbotModel).where(ChatbotModel.unique_id == request.chatbot_unique_id)
        )
        chatbot = result.scalars().first()

        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")

        if chatbot.status != ChatbotStatus.ACTIVE:
            raise HTTPException(status_code=400, detail="Chatbot is not active")

        # Retrieve associated file contents explicitly
        result_files = await session.execute(
            select(FileModel.content)
            .join(chatbots_files, FileModel.id == chatbots_files.c.files_id)
            .where(chatbots_files.c.chatbot_model_id == chatbot.id)
        )
        file_contents = [row[0] for row in result_files]

        # Retrieve previous conversation history
        result_chats = await session.execute(
            select(ChatModel)
            .where(
                ChatModel.chatbot_id == chatbot.id,
                ChatModel.email == request.email,
                ChatModel.phone == request.phone,
            )
            .order_by(ChatModel.created_at.asc())
        )
        chat_history = result_chats.scalars().all()

        # Build conversation history
        conversation_history = [
            {"role": "user", "content": chat.query}
            if chat.response is None else
            {"role": "assistant", "content": chat.response}
            for chat in chat_history
        ]

        # Include the system prompt with title, description, and file contents
        system_content = f"Title: {chatbot.name}\nDescription: {chatbot.description}"
        if file_contents:
            system_content += "\nFiles:\n" + "\n".join(file_contents)
        conversation_history.insert(0, {"role": "system", "content": system_content})

        # Add the user's new message
        conversation_history.append({"role": "user", "content": request.message})

        # Process the conversation
        async def chat_generator():
            assistant_response = ""
            try:
                async for chunk in gpt_completions(conversation_history, BotData(chatbot)):
                    assistant_response += chunk
                    yield chunk

                # Save the new chat interaction
                new_chat = ChatModel(
                    chatbot_id=chatbot.id,
                    email=request.email,
                    phone=request.phone,
                    query=request.message,
                    response=assistant_response,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                session.add(new_chat)
                await session.commit()
            except Exception as e:
                logger.error(f"Error during chat generation: {e}")
                yield "An error occurred while generating the response."

        return StreamingResponse(chat_generator(), media_type="text/plain")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in /chatbot endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail="An error occurred")

# Endpoint to get existing chat history
@app.post("/chat_history", operation_id="getChatHistory")
async def get_chat_history(
    request: ChatHistoryRequest, session: AsyncSession = Depends(get_db)
):

    try:
        # Retrieve the chatbot by unique_id
        result = await session.execute(
            select(ChatbotModel).where(
                ChatbotModel.unique_id == request.chatbot_unique_id
            )
        )
        chatbot = result.scalars().first()
        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")

        # Retrieve chat history for the given email and phone
        result = await session.execute(
            select(ChatModel)
            .where(
                ChatModel.chatbot_id == chatbot.id,
                ChatModel.email == request.email,
                ChatModel.phone == request.phone,
            )
            .order_by(ChatModel.created_at.asc())
        )
        chat_history = result.scalars().all()

        # Construct the conversation history
        conversation_history = [
            {
                "query": chat.query,
                "response": chat.response,
                "createdAt": chat.created_at.isoformat(),
                "updatedAt": chat.updated_at.isoformat(),
            }
            for chat in chat_history
        ]

        return {"chat_history": conversation_history}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving chat history: {str(e)}")
        raise HTTPException(
            status_code=500, detail="An error occurred while retrieving chat history"
        )

# Unified Chatbot Webhook Endpoint
@app.api_route(
    "/webhook/chatbot/{unique_id}", 
    methods=["GET", "POST"],
    operation_id="chatbotWebhookHandler"
)
async def chatbot_webhook_handler(
    unique_id: str, 
    request: Request, 
    # x_secret_token: str = Header(None),
    session: AsyncSession = Depends(get_db)
):
    """Unified webhook to handle incoming Chatbot messages and verification."""
    # Verify secret token
    # expected_token = os.getenv("CHATBOT_WEBHOOK_SECRET_TOKEN")
    # if x_secret_token != expected_token:
    #     logger.warning("Forbidden chatbot webhook access attempt.")
    #     return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        # Get the chatbot by unique_id
        result = await session.execute(
            select(ChatbotModel).where(ChatbotModel.unique_id == unique_id)
        )
        chatbot = result.scalars().first()
        if not chatbot:
            return JSONResponse({"error": "Chatbot not found"}, status_code=404)

        if request.method == "GET":
            # Handle verification
            return await handle_verification(request, chatbot.verify_token)
        elif request.method == "POST":
            # Handle incoming messages
            body = await request.json()
            logger.info(f"Chatbot Webhook received: {body}")

            # Extract the message(s)
            entry = body.get("entry", [])
            for entry_item in entry:
                changes = entry_item.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for message in messages:
                        await handle_chatbot_message(message, chatbot, session)
            return JSONResponse({"status": "OK"}, status_code=200)
    except Exception as e:
        logger.error(f"Error processing Chatbot webhook: {e}")
        return JSONResponse(
            {"error": "Internal server error"}, status_code=500
        )

@app.get("/messages", operation_id="getHistoricalMessages")
async def get_historical_messages(
    whatsappId: int,
    contactId: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
    redis_conn: redis.Redis = Depends(get_redis_client),
):
    """
    Retrieve historical messages for a specific WhatsApp and contact.

    - **whatsappId**: ID of the WhatsApp instance.
    - **contactId**: ID of the contact.
    - **limit**: Number of messages to retrieve (default: 50, max: 100).
    - **offset**: Starting index for messages (default: 0).
    """
    try:
        result = await session.execute(
            select(WhatsappMessageModel)
            .where(
                WhatsappMessageModel.whatsapp_id == whatsappId,
                WhatsappMessageModel.contact_id == contactId,
            )
            .order_by(WhatsappMessageModel.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        messages = result.scalars().all()

        # Fetch media URLs for messages with attachments
        for message in messages:
            if message.media_id:
                media_url = await get_media_url(message.media_id, message.whatsapp_id, session)
                message.media_url = media_url  # Add a dynamic attribute

        # Construct the response
        conversation_history = [
            {
                "id": message.id,
                "whatsappId": message.whatsapp_id,
                "contactId": message.contact_id,
                "direction": message.direction.value,
                "messageContent": message.message_content,
                "mediaId": message.media_id,
                "mediaType": message.media_type,
                "mediaUrl": getattr(message, 'media_url', None),  # Include media URL if available
                "caption": message.caption,
                "status": message.status.value,
                "createdAt": message.created_at.isoformat(),
                "updatedAt": message.updated_at.isoformat(),
            }
            for message in messages
        ]

        return {"chat_history": conversation_history}

    except Exception as e:
        logger.error(f"Error retrieving historical messages: {e}")
        raise HTTPException(
            status_code=500,
            detail="An error occurred while retrieving historical messages.",
        )

# Send a new message
@app.post("/send_message", operation_id="sendMessage")
async def send_message(
    whatsappId: int = Form(...),
    contactId: int = Form(...),
    messageContent: str = Form(...),
    direction: str = Form(...),
    attachment: UploadFile = File(None),
    session: AsyncSession = Depends(get_db),
    redis_conn: redis.Redis = Depends(get_redis_client),
):
    """
    Send a new message with an optional attachment and save it in the database.
    """
    try:
        logger.info(f"Sending message from WhatsApp ID {whatsappId} to Contact ID {contactId}")
        # Ensure contact exists
        result = await session.execute(
            select(ContactModel).where(ContactModel.id == contactId)
        )
        contact = result.scalars().first()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

        # Ensure WhatsApp credentials exist
        result = await session.execute(
            select(WhatsappModel).where(WhatsappModel.id == whatsappId)
        )
        whatsapp = result.scalars().first()
        if not whatsapp:
            raise HTTPException(
                status_code=404, detail="WhatsApp credentials not found"
            )

        media_id = None
        media_type = None
        caption = None

        print(attachment)

        if attachment:
            # Define supported MIME types
            supported_media_types = {
                "image/jpeg": "image",
                "image/png": "image",
                "image/webp": "image",
                "audio/aac": "audio",
                "audio/mp4": "audio",
                "audio/mpeg": "audio",
                "audio/amr": "audio",
                "audio/ogg": "audio",
                "audio/opus": "audio",
                "application/vnd.ms-powerpoint": "document",
                "application/msword": "document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
                "application/pdf": "document",
                "text/plain": "document",
                "application/vnd.ms-excel": "document",
                "video/mp4": "video",
                "video/3gpp": "video"
            }

            # Save the attachment temporarily
            temp_dir = "/home/fastapi/temp"
            os.makedirs(temp_dir, exist_ok=True)
            unique_filename = f"{uuid.uuid4()}_{attachment.filename}"
            file_path = os.path.join(temp_dir, unique_filename)

            async with aiofiles.open(file_path, 'wb') as out_file:
                content = await attachment.read()
                await out_file.write(content)

            # Detect MIME type using file_path
            mime_type = get_mime_type(file_path)
            logger.info(f"Detected MIME type: {mime_type} for file: {file_path}")

            # Validate MIME type
            if mime_type not in supported_media_types:
                error_message = f"Unsupported attachment type: {mime_type}"
                logger.error(error_message)
                raise HTTPException(status_code=400, detail=error_message)

            media_type = supported_media_types[mime_type]

            # Upload media to WhatsApp and get media_id
            whatsapp_message = SendWhatsAppMessage(
                recipient_id=contact.phone,  # Set recipient_id correctly
                phone_id=whatsapp.phone_id,
                access_token=whatsapp.access_token,
                version=whatsapp.version
            )
            
            media_id = await whatsapp_message.upload_media(media_type, file_path, attachment.filename)

            # Set caption if applicable
            if messageContent:
                caption = messageContent
            else:
                caption = None
                messageContent = ""

        # Send the message via WhatsApp API
        send_whatsapp_message = SendWhatsAppMessage(
            recipient_id=contact.phone,
            message=messageContent,
            phone_id=whatsapp.phone_id,
            access_token=whatsapp.access_token,
            version=whatsapp.version,
        )

        if media_id:
            send_result = await send_whatsapp_message.send_media_message(
                media_type=media_type,
                media_id=media_id,
                caption=caption
            )
        else:
            send_result = await send_whatsapp_message.send_message()

        # Save the message
        new_message = WhatsappMessageModel(
            whatsapp_id=whatsappId,
            contact_id=contactId,
            direction=direction,
            message_content=messageContent,
            media_id=media_id,
            media_type=media_type,
            caption=caption,
            status=MessageStatus.SENT if "Message sent" in send_result else MessageStatus.FAILED,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(new_message)
        await session.commit()
        await session.refresh(new_message)

        # Prepare response data
        response = {
            "id": new_message.id,
            "whatsappId": new_message.whatsapp_id,
            "contactId": new_message.contact_id,
            "direction": new_message.direction.value,
            "messageContent": new_message.message_content,
            "mediaId": new_message.media_id,
            "mediaType": new_message.media_type,
            "caption": new_message.caption,
            "status": new_message.status.value,
            "createdAt": new_message.created_at.isoformat(),
            "updatedAt": new_message.updated_at.isoformat(),
            "result": send_result,
        }

        # Update status in Redis
        task_id = str(uuid.uuid4())
        status_key = f"task:{task_id}:status"
        await redis_conn.hset(status_key, f"record_0", "pending")

        # After sending the message, update Redis status
        new_status = "sent" if "Message sent" in send_result else "failed"
        await redis_conn.hset(status_key, f"record_0", new_status)
        await redis_conn.set(f"task:{task_id}:complete", "true")

        return JSONResponse(content=response, status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        return JSONResponse(
            content={"error": f"Failed to send message: {str(e)}"},
            status_code=500,
        )

# Unified WhatsApp Webhook Endpoint
@app.api_route(
    "/webhook/whatsapp/{unique_id}", 
    methods=["GET", "POST"],
    operation_id="whatsappWebhookHandler"
)
async def whatsapp_webhook_handler(
    unique_id: str, 
    request: Request, 
    # x_secret_token: str = Header(None),
    session: AsyncSession = Depends(get_db)
):
    """Unified webhook to handle incoming WhatsApp messages and verification."""
    # Verify secret token (if re-enabled)
    # expected_token = os.getenv("WEBHOOK_SECRET_TOKEN")
    # if x_secret_token != expected_token:
    #     logger.warning("Forbidden webhook access attempt.")
    #     return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        # Get the associated WhatsApp configuration
        result = await session.execute(
            select(WhatsappModel).where(WhatsappModel.verify_token == unique_id)
        )
        whatsapp = result.scalars().first()
        if not whatsapp:
            return JSONResponse({"error": "Whatsapp not found"}, status_code=404)

        if request.method == "GET":
            # Handle verification
            return await handle_verification(request, whatsapp.verify_token)
        elif request.method == "POST":
            # Handle incoming messages
            body = await request.json()
            logger.info(f"Webhook received: {body}")

            # Extract the message(s)
            entry = body.get("entry", [])
            for entry_item in entry:
                changes = entry_item.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    contacts = value.get("contacts", [])
                    profile_name = None
                    # Access the name in the contacts
                    for contact in contacts:
                        profile_name = contact.get("profile", {}).get("name", None)
                        logger.debug(f"Contact Name: {profile_name}")  # Use logger

                    # Process the messages
                    for message in messages:
                        await handle_general_reply(message, whatsapp, profile_name, session)
            return JSONResponse({"status": "OK"}, status_code=200)
    except Exception as e:
        logger.error(f"Error processing WhatsApp webhook: {e}")
        return JSONResponse(
            {"error": "Internal server error"}, status_code=500
        )

# Helper function to broadcast messages to connected clients via WebSocket
async def broadcast_message(contact_id: int, message: str):
    """
    Broadcast a message to all WebSocket clients subscribed to the given contact_id.
    
    - **contact_id**: ID of the contact.
    - **message**: JSON-encoded message string.
    """
    try:
        await manager.broadcast(contact_id, message)
    except Exception as e:
        logger.error(f"Failed to broadcast message to contact {contact_id}: {e}")

# Handle general replies from recipients
async def handle_general_reply(message: dict, whatsapp: WhatsappModel, profile_name: str, session: AsyncSession):
    """Process incoming WhatsApp replies from recipients."""
    try:
        sendar = message.get("from")  # Sender's WhatsApp number
        sender_name = profile_name or sendar
        message_body = message.get("text", {}).get("body", "").strip()
        timestamp = datetime.fromtimestamp(int(message.get("timestamp")))
        logger.info(f"Chatbot message received from {sendar}: {message_body}")

        # Set recipient_replied to True in Redis
        recipient_replied_key = f"recipient_replied:{sendar}"
        redis_client_sync.set(recipient_replied_key, "1")

        # Fetch or create the contact
        result = await session.execute(
            select(ContactModel).where(ContactModel.phone == sendar)
        )
        contact = result.scalars().first()

        if not contact:
            # Create a new contact
            contact = ContactModel(
                user_id=whatsapp.user_id,  # Set appropriate user_id
                name=sender_name,
                phone=sendar,
                email="",
                country="",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(contact)
            await session.commit()
            await session.refresh(contact)

        # Save the incoming message
        new_message = WhatsappMessageModel(
            whatsapp_id=whatsapp.id,
            contact_id=contact.id,
            direction=MessageDirection.CONTACT_TO_WHATSAPP,
            message_content=message_body,
            status=MessageStatus.RECEIVED,
            created_at=timestamp,
            updated_at=datetime.utcnow(),
        )
        session.add(new_message)
        await session.commit()
        await session.refresh(new_message)

        logger.info(f"Saved general reply from {sendar}: {message_body}")

        # Prepare message data
        message_data = json.dumps({
            "id": new_message.id,
            "whatsappId": new_message.whatsapp_id,
            "contactId": new_message.contact_id,
            "direction": new_message.direction.value,
            "messageContent": new_message.message_content,
            "status": new_message.status.value,
            "createdAt": new_message.created_at.isoformat(),
            "updatedAt": new_message.updated_at.isoformat(),
        })

        # Broadcast the message via WebSocket
        await broadcast_message(contact.id, message_data)

    except Exception as e:
        logger.error(f"Error handling general reply: {e}")

# WebSocket Endpoint
@app.websocket("/ws/{contact_id}")
async def websocket_endpoint(websocket: WebSocket, contact_id: int):
    logger.info(f"Attempting WebSocket connection for contact_id {contact_id}")
    await manager.connect(websocket, contact_id)
    try:
        logger.info(f"WebSocket connection established for contact_id {contact_id}")
        while True:
            data = await websocket.receive_text()
            logger.debug(f"Received message from client: {data}")
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for contact_id {contact_id}")
        manager.disconnect(websocket, contact_id)
    except Exception as e:
        logger.error(f"WebSocket connection error for contact_id {contact_id}: {e}")
        manager.disconnect(websocket, contact_id)


async def handle_automation_reply(
    message: dict,
    automation: AutomationModel,
    session: AsyncSession
):
    """
    Process incoming replies from recipients, updating 
    existing automation data that was previously stored in Redis.
    """
    try:
        # 1. Extract data from the incoming message
        sender_phone = message.get("from")  # e.g. "2348012345678"
        message_body = message.get("text", {}).get("body", "").strip()
        timestamp_unix = message.get("timestamp", 0)
        timestamp = datetime.fromtimestamp(int(timestamp_unix))

        logger.info(f"Automation reply received from {sender_phone}: {message_body}")

        # 2. Update Redis to record that this phone has replied
        #    Suppose you previously stored everything under "batch:<batch_id>"
        batch_key = f"batch:{automation.batch_id}"
        phone_replied_field = f"replied_{sender_phone}"

        redis_client_sync.hset(batch_key, phone_replied_field, "true")
        redis_client_sync.hset(batch_key, f"last_reply_timestamp_{sender_phone}", str(timestamp))
        redis_client_sync.hset(batch_key, f"last_reply_body_{sender_phone}", message_body)

        # 3. (Optional) Update or log something in your database
        # For example, if your AutomationModel has a 'last_reply' field:
        # automation.last_reply = timestamp
        # automation.last_reply_phone = sender_phone
        # session.add(automation)
        # await session.commit()

        # 4. (Optional) Broadcast or notify other parts of your system
        # For instance, if you have a queue or WebSocket you want to update:
        # message_data = {
        #     "phone": sender_phone,
        #     "timestamp": timestamp.isoformat(),
        #     "body": message_body
        # }
        # await some_broadcast_function(json.dumps(message_data))

        logger.info(f"Redis updated for batch {automation.batch_id} - phone {sender_phone} replied.")

    except Exception as e:
        logger.error(f"Error handling automation reply: {e}", exc_info=True)

@app.api_route(
    "/webhook/automation/{unique_id}",
    methods=["GET", "POST"],
    operation_id="automationWebhookHandler"
)
async def automation_webhook_handler(
    unique_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db)
):
    """
    Unified webhook to handle incoming automation requests and verification.
    - `unique_id` can be a token stored in your AutomationModel for verification.
    - GET requests may be used for verifying the webhook.
    - POST requests handle incoming data (like messages, statuses, etc.).
    """
    try:
        # ----------------------------
        # 1. Fetch AutomationModel entry
        # ----------------------------
        result = await session.execute(
            select(AutomationModel).where(AutomationModel.verify_token == unique_id)
        )
        automation = result.scalars().first()
        if not automation:
            # If no matching automation config found, respond with 404
            logger.warning(f"No automation config found for token: {unique_id}")
            return JSONResponse({"error": "Automation config not found"}, status_code=404)

        # ----------------------------
        # 2. Handle GET (verification) or POST (incoming data)
        # ----------------------------
        if request.method == "GET":
            return await handle_verification(request, automation.verify_token)

        elif request.method == "POST":
            # Handle incoming messages
            body = await request.json()
            logger.info(f"Webhook received: {body}")

            # Extract the message(s)
            entry = body.get("entry", [])
            for entry_item in entry:
                changes = entry_item.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    contacts = value.get("contacts", [])
                    profile_name = None
                    # Access the name in the contacts
                    for contact in contacts:
                        profile_name = contact.get("profile", {}).get("name", None)
                        logger.debug(f"Contact Name: {profile_name}")  # Use logger

                    # Process the messages
                    for message in messages:
                        await handle_automation_reply(message, automation, profile_name, session)
            return JSONResponse({"status": "OK"}, status_code=200)
    except Exception as e:
        logger.error(f"Error processing WhatsApp webhook: {e}")
        return JSONResponse(
            {"error": "Internal server error"}, status_code=500
        )
    

@app.post("/process_automation")
async def process_auto_file(
    request: AutoFileRequestModel,
    session: AsyncSession = Depends(get_db),
    redis_conn: redis.Redis = Depends(get_redis_client),
):
    """
    Process an auto file for automation (WhatsApp, Email, or Both).
    - auto_file.encrypt_file: a CSV (plain or encrypted) with phone/email rows.
    - auto_file.template_file: a file (pdf/doc/etc.) to send via WhatsApp if needed.
    """

    try:
        # ------------------------------------------------
        # 1. Fetch AutoFileModel
        # ------------------------------------------------
        db_result = await session.execute(
            select(AutoFileModel).where(AutoFileModel.id == request.autoFileId)
        )
        auto_file = db_result.scalars().first()
        if not auto_file:
            raise HTTPException(status_code=404, detail="AutoFile not found.")

        # ------------------------------------------------
        # 2. Fetch AutomationModel
        # ------------------------------------------------
        db_result = await session.execute(
            select(AutomationModel).where(AutomationModel.id == request.automationId)
        )
        automation = db_result.scalars().first()
        if not automation:
            raise HTTPException(status_code=404, detail="Automation configuration not found.")

        # ------------------------------------------------
        # 3. Set Redis meta info for the batch
        # ------------------------------------------------
        batch_id = auto_file.batch_id
        batch_name = auto_file.batch_name

        meta_key = f"batch:{batch_id}:meta"
        await redis_conn.hset(
            meta_key,
            mapping={
                "batchId": batch_id,
                "batchName": batch_name,
                "status": "sending",  # The batch is now in 'sending' state
            }
        )

        # Rows key if you decide to store row-level data in a single place
        # (But your code below still references batch:{batch_id}:row:{i} separately.)
        rows_key = f"batch:{batch_id}:rows"

        # ------------------------------------------------
        # 4. Parse the CSV file (plain or encrypted)
        # ------------------------------------------------
        csv_path = auto_file.encrypt_file

        # Attempt to read file contents
        try:
            with open(csv_path, "rb") as f:
                csv_contents = f.read()
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"CSV file not found on disk: {csv_path}"
            )

        rows = []
        # Try parsing as plain CSV first
        try:
            plain_csv = io.StringIO(csv_contents.decode("utf-8"))
            reader = csv.DictReader(plain_csv)
            rows = list(reader)
            if not rows or reader.fieldnames is None:
                raise ValueError("No valid rows found in plain CSV attempt.")
        except Exception:
            # If plain CSV fails, attempt to decrypt and parse again
            try:
                decrypted_bytes = decrypt_file(csv_contents, auto_file.encrypt_key)
                decrypted_csv = io.StringIO(decrypted_bytes.decode("utf-8"))
                reader = csv.DictReader(decrypted_csv)
                rows = list(reader)
                if not rows or reader.fieldnames is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Failed to parse CSV after attempting decryption."
                    )
            except Exception as e2:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to parse CSV or decrypt: {e2}"
                )

        # ------------------------------------------------
        # 5. Check MIME type for template_file (WhatsApp)
        # ------------------------------------------------
        template_path = auto_file.template_file
        media_type = None
        if request.toolType in (toolType.WHATSAPP, toolType.BOTH):
            supported_media_types = {
                "image/jpeg": "image",
                "image/png": "image",
                "image/webp": "image",
                "audio/aac": "audio",
                "audio/mp4": "audio",
                "audio/mpeg": "audio",
                "audio/amr": "audio",
                "audio/ogg": "audio",
                "audio/opus": "audio",
                "application/vnd.ms-powerpoint": "document",
                "application/msword": "document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
                "application/pdf": "document",
                "text/plain": "document",
                "application/vnd.ms-excel": "document",
                "video/mp4": "video",
                "video/3gpp": "video",
            }
            try:
                mime_type = get_mime_type(template_path)
                logger.info(f"Detected MIME type: {mime_type} for file: {template_path}")
                if mime_type not in supported_media_types:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsupported attachment type: {mime_type}"
                    )
                media_type = supported_media_types[mime_type]
            except FileNotFoundError:
                raise HTTPException(
                    status_code=404,
                    detail=f"Template file not found on disk: {template_path}"
                )

        # ------------------------------------------------
        # 6. Store initial row-level info in Redis
        # ------------------------------------------------
        pipe = redis_conn.pipeline()
        for i, row in enumerate(rows):
            row_key = f"batch:{batch_id}:row:{i}"
            record_key = f"record_{i}"

            # Prepare row data
            row_data = {
                "index": i,
                "key": record_key,
                "batchId": batch_id,
                "batchName": batch_name,
                "status": "pending",   # overall row status (pending, success, or failed)
                "whatsapp": "none",    # sent/failed/none
                "email": "none",       # sent/failed/none
                "phone": row.get("phone", ""),
                "emailAddress": row.get("email", ""),
            }

            # Convert to JSON for easy retrieval
            pipe.set(row_key, json.dumps(row_data))
        await pipe.execute()

        # Track overall success/failure for each channel
        whatsapp_result_overall = True if request.toolType in (toolType.WHATSAPP, toolType.BOTH) else None
        email_result_overall = True if request.toolType in (toolType.EMAIL, toolType.BOTH) else None

        # ------------------------------------------------
        # 7. Process each row (WhatsApp & Email)
        # ------------------------------------------------
        for i, csv_row in enumerate(rows):
            row_key = f"batch:{batch_id}:row:{i}"

            # Load current row data from Redis
            row_data_str = await redis_conn.get(row_key)
            if not row_data_str:
                logger.error(f"No row data found in Redis for key: {row_key}")
                continue

            row_data = json.loads(row_data_str)
            row_status = "success"  # assume success until we hit an error

            # 7a. WhatsApp
            if request.toolType in (toolType.WHATSAPP, toolType.BOTH):
                phone = csv_row.get("phone")
                if phone:
                    try:
                        whatsapp_msg = SendWhatsAppMessage(
                            recipient_id=phone,
                            phone_id=automation.phone_id,
                            access_token=automation.access_token,
                            version=automation.version
                        )
                        media_id = None
                        if media_type:
                            media_id = await whatsapp_msg.upload_media(
                                media_type,
                                template_path,
                                batch_name
                            )

                        template_name = request.templateType.value.lower()
                        # Attempt sending
                        await whatsapp_msg.send_template_message(
                            template_name,
                            media_id,
                            batch_name
                        )
                        row_data["whatsapp"] = "sent"

                    except Exception as exc:
                        logger.error(f"Failed to send WhatsApp to {phone}: {exc}", exc_info=True)
                        whatsapp_result_overall = False
                        row_data["whatsapp"] = "failed"
                        row_status = "failed"
                else:
                    row_data["whatsapp"] = "none"

            # 7b. Email
            if request.toolType in (toolType.EMAIL, toolType.BOTH):
                email_address = csv_row.get("email")
                if email_address:
                    try:
                        await send_batch_email(
                            automation,
                            email_address,
                            batch_name,
                            attachment_paths=[template_path]
                        )
                        row_data["email"] = "sent"
                    except Exception as exc:
                        logger.error(f"Failed to send email to {email_address}: {exc}", exc_info=True)
                        email_result_overall = False
                        row_data["email"] = "failed"
                        row_status = "failed"
                else:
                    row_data["email"] = "none"

            # Update final row status
            row_data["status"] = row_status

            # Save the updated data back to Redis
            await redis_conn.set(row_key, json.dumps(row_data))

        # Mark the entire "task" as complete in Redis
        task_id = str(uuid.uuid4())
        await redis_conn.set(f"task:{task_id}:complete", "true")

        # ------------------------------------------------
        # 8. Determine final batch status
        # ------------------------------------------------
        final_status = "completed"
        if request.toolType in (toolType.WHATSAPP, toolType.BOTH) and whatsapp_result_overall is False:
            final_status = "failed"
        if request.toolType in (toolType.EMAIL, toolType.BOTH) and email_result_overall is False:
            final_status = "failed"

        # Update batch meta status
        await redis_conn.hset(meta_key, "status", final_status)

        return JSONResponse(
            content={
                "message": "Batch processed successfully",
                "batchId": batch_id,
                "batchName": batch_name,
                "status": final_status,
            },
            status_code=200,
        )

    except HTTPException as http_err:
        # Explicitly raise known HTTPExceptions
        raise http_err

    except Exception as exc:
        # Catch-all for unexpected errors
        logger.error(f"Unexpected error during batch {batch_id}: {exc}", exc_info=True)
        if "auto_file" in locals() and auto_file and batch_id:
            # Mark batch as failed if possible
            await redis_conn.hset(f"batch:{batch_id}:meta", "status", "failed")

        return JSONResponse(
            content={"error": str(exc)},
            status_code=500,
        )

@app.get("/batch_status")
async def get_all_batches_status(
    offset: int = Query(0, ge=0),   # pagination offset
    limit: int = Query(10, ge=1),   # pagination limit
    redis_conn: redis.Redis = Depends(get_redis_client)
):
    """
    Retrieve status for ALL batches in Redis, paginated.
    
    - Scans for keys matching:   batch:*:meta
    - For each meta key, we parse out the batchId (and other details).
    - Then for each batch, we gather row-level stats from keys:
        batch:{batchId}:row:*
    - Returns aggregated stats: total rows, # sent/failed, etc.
    
    Query Params:
      - offset: starting index of the batch list (default=0)
      - limit: number of batches to return per page (default=10)
    """

    # -----------------------------------------------------------------
    # 1. Find all meta keys -> one meta key per batch
    #    For large scale, you would do a SCAN-based approach.
    # -----------------------------------------------------------------
    meta_key_pattern = "batch:*:meta"
    meta_keys = await redis_conn.keys(meta_key_pattern)  # list of keys like ["batch:123:meta", "batch:999:meta", ...]
    total_batches = len(meta_keys)

    if total_batches == 0:
        return {
            "results": [],
            "offset": offset,
            "limit": limit,
            "total": 0
        }

    # -----------------------------------------------------------------
    # 2. Paginate the meta keys
    # -----------------------------------------------------------------
    # Sort the meta keys to have a consistent ordering (optional).
    meta_keys.sort()
    
    # If offset >= total, return empty result set
    if offset >= total_batches:
        return {
            "results": [],
            "offset": offset,
            "limit": limit,
            "total": total_batches
        }

    # Slice the meta keys for this page
    selected_keys = meta_keys[offset : offset + limit]

    # Prepare a list to hold each batch’s aggregated info
    results = []

    # -----------------------------------------------------------------
    # 3. For each meta key, collect stats
    # -----------------------------------------------------------------
    for meta_key in selected_keys:
        # Example: meta_key = "batch:366204:meta"
        meta_data = await redis_conn.hgetall(meta_key)
        if not meta_data:
            # If no meta found, skip
            continue

        # Decode if your Redis connection does not use decode_responses=True
        # meta_data = {k.decode(): v.decode() for k, v in meta_data.items()}

        # Extract batch ID/name from the meta data
        batch_id = meta_data.get("batchId")
        if not batch_id:
            # You could parse from key name if needed, e.g., split on ":"
            continue

        batch_name = meta_data.get("batchName", "Unknown")
        batch_status = meta_data.get("status", "unknown")

        # ---------------------------------------------------------
        # 3a. Find row keys for this batch
        # ---------------------------------------------------------
        row_key_pattern = f"batch:{batch_id}:row:*"
        row_keys = await redis_conn.keys(row_key_pattern)  # e.g. ["batch:366204:row:0", "batch:366204:row:1", ...]

        # ---------------------------------------------------------
        # 3b. Initialize counters
        # ---------------------------------------------------------
        stats = {
            "total_rows": 0,
            "valid_whatsapp": 0,  # # of rows that have a phone
            "valid_email": 0,     # # of rows that have an email
            "whatsapp_sent": 0,
            "whatsapp_failed": 0,
            "email_sent": 0,
            "email_failed": 0,
            "row_success": 0,     # # of rows with status=success
            "row_failed": 0,      # # of rows with status=failed
        }

        # ---------------------------------------------------------
        # 3c. Parse each row and update stats
        # ---------------------------------------------------------
        for row_key in row_keys:
            row_data_raw = await redis_conn.get(row_key)
            if not row_data_raw:
                continue

            row_data = json.loads(row_data_raw)

            stats["total_rows"] += 1

            phone = row_data.get("phone", "")
            if phone:
                stats["valid_whatsapp"] += 1

            email_address = row_data.get("emailAddress", "")
            if email_address:
                stats["valid_email"] += 1

            # WhatsApp status
            whatsapp_status = row_data.get("whatsapp", "none")
            if whatsapp_status == "sent":
                stats["whatsapp_sent"] += 1
            elif whatsapp_status == "failed":
                stats["whatsapp_failed"] += 1

            # Email status
            email_status = row_data.get("email", "none")
            if email_status == "sent":
                stats["email_sent"] += 1
            elif email_status == "failed":
                stats["email_failed"] += 1

            # Overall row status
            row_status = row_data.get("status", "pending")
            if row_status == "success":
                stats["row_success"] += 1
            elif row_status == "failed":
                stats["row_failed"] += 1

        # ---------------------------------------------------------
        # 3d. Build the result object for this batch
        # ---------------------------------------------------------
        batch_info = {
            "batchId": batch_id,
            "batchName": batch_name,
            "batchStatus": batch_status,
            "stats": stats,
        }
        results.append(batch_info)

    # -----------------------------------------------------------------
    # 4. Return the paginated results
    # -----------------------------------------------------------------
    return {
        "results": results,
        "offset": offset,
        "limit": limit,
        "total": total_batches
    }


# Entry point for running the app
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
