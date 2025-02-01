import asyncio
import json
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

# Import necessary models and functions
from enums import ChatbotStatus, CountryEnum, MessageDirection, MessageStatus
from model import (
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
from assistant import BotData, SendWhatsAppMessage, gpt_completions, redis_client_sync
from model import chatbots_files
from redis_client import get_redis_client

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Handle WhatsApp webhook verification
async def handle_verification(request: Request, verify_token: str):
    """Handle WhatsApp webhook verification."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == verify_token:
        logger.info("Webhook verification successful.")
        return PlainTextResponse(content=challenge, status_code=200)
    else:
        logger.warning("Invalid token or mode during verification.")
        return PlainTextResponse(content="Forbidden", status_code=403)

# Process incoming WhatsApp messages for chatbot
async def handle_chatbot_message(
    message: dict, chatbot: ChatbotModel, session: AsyncSession
):
    """Process incoming WhatsApp messages intended for the chatbot."""
    try:
        sendar = message.get("from")  # Sender's WhatsApp number
        message_body = message.get("text", {}).get("body", "")
        timestamp = datetime.fromtimestamp(int(message.get("timestamp")))
        logger.info(f"Chatbot message received from {sendar}: {message_body}")

        # Set recipient_replied to True in Redis
        recipient_replied_key = f"recipient_replied:{sendar}"
        await redis_client_sync.set(recipient_replied_key, "1")

        # Retrieve previous conversation history from ChatModel
        result = await session.execute(
            select(ChatModel)
            .where(
                ChatModel.chatbot_id == chatbot.id,
                ChatModel.phone == sendar,
            )
            .order_by(ChatModel.created_at.asc())
        )
        chat_history = result.scalars().all()

        conversation_history = []
        for chat in chat_history:
            conversation_history.append({"role": "user", "content": chat.query})
            if chat.response:
                conversation_history.append(
                    {"role": "assistant", "content": chat.response}
                )

        # Include the system prompt with title, description, and file contents
        file_contents = [file.content for file in chatbot.files]
        system_content = f"Title: {chatbot.name}\nDescription: {chatbot.description}"
        if file_contents:
            system_content += "\nFiles:\n" + "\n".join(file_contents)
        conversation_history.insert(0, {"role": "system", "content": system_content})

        # Add the user's new message
        conversation_history.append(
            {"role": "user", "content": message_body.strip()}
        )

        # Prepare bot_data
        bot_data = BotData(chatbot)

        # Call gpt_completions to get assistant's response
        assistant_response = ""
        async for chunk in gpt_completions(conversation_history, bot_data):
            assistant_response += chunk

        if assistant_response.strip():
            # Send the response back via WhatsApp
            whatsapp_message = SendWhatsAppMessage(
                recipient_id=sendar,
                message=assistant_response.strip(),
                phone_id=chatbot.phone_id,
                access_token=chatbot.access_token,
                version=chatbot.version,
            )
            await whatsapp_message.send_message()

            # Save the new chat interaction
            new_chat = ChatModel(
                chatbot_id=chatbot.id,
                email="",
                phone=sendar,
                query=message_body.strip(),
                response=assistant_response.strip(),
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(new_chat)
            await session.commit()
        else:
            logger.warning("Assistant response is empty. Sending fallback message.")
            fallback_message = SendWhatsAppMessage(
                recipient_id=sendar,
                message="Sorry, I couldn't understand that.",
                phone_id=chatbot.phone_id,
                access_token=chatbot.access_token,
                version=chatbot.version,
            )
            await fallback_message.send_message()

    except Exception as e:
        logger.error(f"Message handling error: {e}")
        # Optionally, send an error message back to the user
        fallback_message = SendWhatsAppMessage(
            recipient_id=sendar,
            message="An error occurred while processing your message.",
            phone_id=chatbot.phone_id,
            access_token=chatbot.access_token,
            version=chatbot.version,
        )
        await fallback_message.send_message()