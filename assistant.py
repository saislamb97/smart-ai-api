import os
import logging
import json
import asyncio
import re

import aiofiles
import aiohttp
import redis
from openai import AsyncOpenAI
import tiktoken
from pydantic import BaseModel

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException

# Import models and functions from model.py
from helper import get_mime_type
from model import ChatbotModel, WhatsappModel, ChatbotCreativity

client = AsyncOpenAI()

# Configure OpenAI API key
client.api_key = os.getenv("OPENAI_API_KEY")

# Retrieve max_tokens from environment variable, converting to int
MAX_TOKENS = int(os.getenv("MAX_TOKENS", 16000))

# Set up Redis client
REDIS_URL = f"redis://{os.getenv('REDIS_HOST')}:{os.getenv('REDIS_PORT')}/{os.getenv('REDIS_DB')}"
redis_client_sync = redis.Redis.from_url(REDIS_URL)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Function to count total number of tokens.
encoding = tiktoken.get_encoding("cl100k_base")
def num_tokens_from_messages(messages, tokens_per_message=3, tokens_per_name=1):
    num_tokens = 0
    max_tokens = MAX_TOKENS

    # Determine the number of tokens used by the given message
    def tokens_used_by_message(message):
        tokens = tokens_per_message
        for key, value in message.items():
            tokens += len(encoding.encode(value))
            if key == "name":
                tokens += tokens_per_name
        return tokens

    # Calculate the total number of tokens in the current list of messages
    for message in messages:
        num_tokens += tokens_used_by_message(message)

    num_tokens += 3

    # While the total tokens exceed the model's maximum context length, remove older messages
    while num_tokens > max_tokens and len(messages) > 1:
        # Remove the oldest message and subtract its tokens from num_tokens
        removed_message = messages.pop(1)
        num_tokens -= tokens_used_by_message(removed_message)

    return messages


# Utility function to extract phone number and message
def extract_phone_and_message(query: str):
    """Extract phone number and message from the query."""
    phone_pattern = r"\+?\d{10,15}"
    phone_match = re.search(phone_pattern, query)
    recipient_id = phone_match.group() if phone_match else None
    message = query.replace(recipient_id, "").strip() if recipient_id else None
    return recipient_id, message


class SendWhatsAppMessage(BaseModel):
    recipient_id: str
    message: str = None  # Optional for media messages
    phone_id: str
    access_token: str
    version: str

    async def send_message(self):
        """Send a text message based on the state stored in Redis."""
        template_sent_key = f"template_sent:{self.recipient_id}"
        recipient_replied_key = f"recipient_replied:{self.recipient_id}"

        # Get the state from Redis
        template_sent = redis_client_sync.get(template_sent_key)
        recipient_replied = redis_client_sync.get(recipient_replied_key)

        # Convert bytes to boolean
        template_sent = template_sent and template_sent.decode('utf-8') == '1'
        recipient_replied = recipient_replied and recipient_replied.decode('utf-8') == '1'

        if not template_sent:
            # Send the template message and set the flag in Redis
            result = await self.send_template_message("greeting")
            redis_client_sync.set(template_sent_key, '1')
            logger.info("Template sent")
        elif not recipient_replied:
            # Keep sending the template until the recipient replies
            result = await self.send_template_message("greeting")
            logger.info("Template sent")
        else:
            # Send a regular text message
            result = await self.send_text_message()
            logger.info("Text message sent")
        return result

    async def send_template_message(self, template_name: str, media_id: str = None, filename: str = None):
        """Send a template message with placeholders."""
        template_payload = {
            "name": template_name,
            "language": {"code": "en"},
            "components": []
        }

        if template_name == "reminder":
            # Add components for the 'reminder' template
            template_payload["components"].append(
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {
                                "id": media_id,
                                "filename": filename
                            }
                        }
                    ]
                }
            )
        elif template_name == "greeting":
            # Add components for the 'hello_world' template
            template_payload["components"].append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Hello, world!"}
                    ]
                }
            )
        
        # Extend for other templates as needed
        
        return await self._send_request("template", template_payload)

    async def send_text_message(self):
        """Send a regular text message."""
        return await self._send_request(
            "text",
            {"preview_url": False, "body": self.message}
        )

    async def send_media_message(self, media_type: str, media_id: str, caption: str = None):
        """Send a media message (image, document, etc.) with an optional caption."""
        content = {"id": media_id}
        if caption:
            content["caption"] = caption

        return await self._send_request(
            media_type,
            content
        )

    async def upload_media(self, media_type: str, file_path: str, filename: str):
        """Upload media to WhatsApp Cloud API and retrieve the media ID."""
        url = f"https://graph.facebook.com/{self.version}/{self.phone_id}/media"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        try:
            logger.info(f"Uploading {media_type}: {filename}")
            form = aiohttp.FormData()
            form.add_field('file', open(file_path, 'rb'))  # Directly pass the file path
            form.add_field('type', media_type)
            form.add_field('messaging_product', 'whatsapp')

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=form) as response:
                    response_data = await response.json()
                    if response.status in [200, 201]:
                        media_id = response_data.get('id')
                        if media_id:
                            logger.info(f"Media uploaded successfully. Media ID: {media_id}")
                            return media_id
                        logger.error("Media ID not found in the response.")
                        return "Error: Media ID not found."
                    else:
                        error_message = response_data.get('error', {}).get('message', 'Unknown error')
                        logger.error(f"Upload failed: {error_message}")
                        return f"Error: {error_message}"
        except Exception as e:
            logger.error(f"Media upload failed: {e}")
            return f"Error: {str(e)}"


    async def _send_request(self, message_type: str, content: dict):
        """Send request to WhatsApp Cloud API asynchronously."""
        url = f"https://graph.facebook.com/{self.version}/{self.phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": self.recipient_id,
            "type": message_type,
            message_type: content,
        }

        try:
            logger.info(f"Sending {message_type} to {self.recipient_id}: {payload}")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    response_text = await response.text()
                    logger.info(f"Response: {response.status} - {response_text}")

                    if response.status in [200, 201]:
                        data = await response.json()
                        return f"Message sent. ID: {data.get('messages', [{}])[0].get('id', 'unknown')}"
                    else:
                        try:
                            error_message = (await response.json()).get('error', {}).get('message', 'Unknown error')
                        except:
                            error_message = "Unknown error during message sending."
                        logger.error(f"Failed to send message: {error_message}")
                        return f"Error: {error_message}"
        except Exception as e:
            logger.error(f"Request Error: {e}")
            return f"An error occurred: {str(e)}"
        
        
async def get_media_url(media_id: str, whatsapp_id: int, session: AsyncSession):
    """
    Retrieve the media URL from WhatsApp Cloud API using the media_id.
    """
    try:
        # Fetch WhatsApp configuration
        result = await session.execute(
            select(WhatsappModel).where(WhatsappModel.id == whatsapp_id)
        )
        whatsapp = result.scalars().first()
        if not whatsapp:
            logger.error(f"WhatsApp configuration not found for ID: {whatsapp_id}")
            return None

        url = f"https://graph.facebook.com/v21.0/{media_id}"
        headers = {
            "Authorization": f"Bearer {whatsapp.access_token}"
        }

        async with aiohttp.ClientSession() as session_http:
            async with session_http.get(url, headers=headers) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    media_url = data.get("url")
                    return media_url
                else:
                    logger.error(f"Failed to retrieve media URL for media_id {media_id}. Status: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"Error retrieving media URL for media_id {media_id}: {e}")
        return None


async def check_whatsapp_status(recipient_id: str, bot_data, session: AsyncSession):
    """Check if the recipient is active on WhatsApp."""
    try:
        # Get the chatbot from the database
        result = await session.execute(
            select(ChatbotModel).where(ChatbotModel.unique_id == bot_data.chatbot_unique_id)
        )
        chatbot = result.scalars().first()
        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")

        # Get the associated WhatsApp configuration
        result = await session.execute(
            select(WhatsappModel).where(WhatsappModel.chatbot_id == chatbot.id)
        )
        whatsapp = result.scalars().first()
        if not whatsapp:
            raise HTTPException(status_code=404, detail="WhatsApp configuration not found for this chatbot")

        url = f"https://graph.facebook.com/{whatsapp.version}/{whatsapp.phone_id}/users/{recipient_id}/presence"
        headers = {
            "Authorization": f"Bearer {whatsapp.access_token}",
            "Content-Type": "application/json"
        }

        try:
            async with aiohttp.ClientSession() as http_session:
                async with http_session.get(url, headers=headers) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        json_response = await response.json()
                        presence = json_response.get('presence') == 'active'
                        logger.info(f"WhatsApp presence for {recipient_id}: {presence}")
                        return presence
                    else:
                        logger.warning(f"Status check failed for {recipient_id}. Response: {response_text}")
                        return False
        except Exception as e:
            logger.error(f"Error checking status: {e}")
            return False
    except Exception as e:
        logger.error(f"Database error: {e}")
        return False

async def send_whatsapp_message(recipient_id, message, bot_data, session: AsyncSession):
    """Send WhatsApp message using the bot's meta service."""
    try:
        # Get the chatbot from the database
        result = await session.execute(
            select(ChatbotModel).where(ChatbotModel.unique_id == bot_data.chatbot_unique_id)
        )
        chatbot = result.scalars().first()
        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")

        # Get the associated WhatsApp configuration
        result = await session.execute(
            select(WhatsappModel).where(WhatsappModel.chatbot_id == chatbot.id)
        )
        whatsapp = result.scalars().first()
        if not whatsapp:
            raise HTTPException(status_code=404, detail="WhatsApp configuration not found for this chatbot")

        # Initialize WhatsApp message handler
        whatsapp_message = SendWhatsAppMessage(
            recipient_id=recipient_id,
            message=message,
            phone_id=whatsapp.phone_id,
            access_token=whatsapp.access_token,
            version=whatsapp.version
        )

        return await whatsapp_message.send_message()
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
        return f"Error: {str(e)}"


async def send_template_message(recipient_id, bot_data, session: AsyncSession):
    """Send a WhatsApp template message."""
    try:
        # Get the chatbot from the database
        result = await session.execute(
            select(ChatbotModel).where(ChatbotModel.unique_id == bot_data.chatbot_unique_id)
        )
        chatbot = result.scalars().first()
        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")

        # Get the associated WhatsApp configuration
        result = await session.execute(
            select(WhatsappModel).where(WhatsappModel.chatbot_id == chatbot.id)
        )
        whatsapp = result.scalars().first()
        if not whatsapp:
            raise HTTPException(status_code=404, detail="WhatsApp configuration not found for this chatbot")

        # Initialize WhatsApp message handler
        whatsapp_message = SendWhatsAppMessage(
            recipient_id=recipient_id,
            message="",  # Empty message for template
            phone_id=whatsapp.phone_id,
            access_token=whatsapp.access_token,
            version=whatsapp.version
        )

        return await whatsapp_message.send_template_message()
    except Exception as e:
        logger.error(f"Error sending WhatsApp template message: {e}")
        return f"Error: {str(e)}"


# Function Mapping for Execution
available_functions = {
    "send_whatsapp_message": send_whatsapp_message,
    "send_template_message": send_template_message,
    "check_whatsapp_status": check_whatsapp_status,
}


# Define Functions for Execution
functions = [
    {
        "name": "send_whatsapp_message",
        "description": "Send a WhatsApp text message.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": "Recipient's phone number."
                },
                "message": {
                    "type": "string",
                    "description": "Message content."
                },
            },
            "required": ["recipient_id", "message"],
        },
    },
    {
        "name": "send_template_message",
        "description": "Send a predefined WhatsApp template message.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": "Recipient's phone number."
                }
            },
            "required": ["recipient_id"],
        },
    },
    {
        "name": "check_whatsapp_status",
        "description": "Check if a recipient is active on WhatsApp.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "string",
                    "description": "Recipient's phone number."
                }
            },
            "required": ["recipient_id"],
        },
    },
]


# Handle GPT Completions with Function Calls
async def gpt_completions(messages, bot_data):

    messages = num_tokens_from_messages(messages)

    """Handle GPT completions with possible function calls."""
    try:
        # Start streaming the response
        response = await client.chat.completions.create(
            model=bot_data.model_name,
            messages=messages,
            functions=functions,
            function_call="auto",
            temperature=bot_data.temperature_value,
            stream=True,
        )
        # Return the asynchronous generator from process_stream
        async for content in process_stream(response, messages, bot_data):
            yield content
    except Exception as e:
        logger.error(f"Error during OpenAI API call: {e}")
        raise


async def process_stream(response, messages, bot_data):
    """Process the response stream and handle function calls."""
    function_call_data = None
    assistant_response = ""
    async for chunk in response:
        # Access the first choice# Access the first choice
        choice = chunk.choices[0]
        delta = choice.delta
        finish_reason = choice.finish_reason

        # Access content
        content = getattr(delta, 'content', None)
        if content:
            assistant_response += content
            # Yield the content to the client for streaming
            yield content

        # Access function_call
        function_call = getattr(delta, 'function_call', None)
        if function_call:
            if not function_call_data:
                function_call_data = {'name': '', 'arguments': ''}
            # Accumulate the function name and arguments
            name = getattr(function_call, 'name', '') or ''
            arguments = getattr(function_call, 'arguments', '') or ''
            function_call_data['name'] += name
            function_call_data['arguments'] += arguments

        # Check the finish reason
        if finish_reason == 'function_call':
            # Stop streaming as a function call is detected
            break

    if function_call_data:
        # Append the assistant's message with function_call to messages
        assistant_message = {
            'role': 'assistant',
            'content': None,
            'function_call': function_call_data,
        }
        messages.append(assistant_message)

        # Execute the function call asynchronously
        function_response = await execute_function_call(function_call_data, bot_data)

        # Append the function's response to messages
        function_message = {
            'role': 'function',
            'name': function_call_data['name'],
            'content': function_response,
        }
        messages.append(function_message)

        # Start a new completion and stream the response
        new_stream = client.chat.completions.create(
            model=bot_data.model_name,
            messages=messages,
            temperature=bot_data.temperature_value,
            stream=True,
        )
        # Continue streaming from the new completion
        for new_chunk in new_stream:
            new_choice = new_chunk.choices[0]
            new_delta = new_choice.delta
            new_content = getattr(new_delta, 'content', None)
            if new_content:
                # Stream content to the client
                yield new_content
    # If no function call, the full response has been streamed

async def execute_function_call(function_call_data, bot_data):
    """Execute the function call and return the function's response."""
    function_name = function_call_data['name']
    if function_name in available_functions:
        function_to_call = available_functions[function_name]
        try:
            args = json.loads(function_call_data['arguments'])
            # Call the function asynchronously
            response = await function_to_call(**args, bot_data=bot_data)
            return response
        except (TypeError, json.JSONDecodeError) as e:
            logger.error(f"Execution error: {e}")
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected error during function execution: {e}")
            return f"Error: {str(e)}"
    else:
        error_message = f"Function {function_name} is not available."
        logger.error(error_message)
        return error_message

class BotData:
    def __init__(self, chatbot):
        self.chatbot_unique_id = chatbot.unique_id
        self.model_name = os.getenv("GPT_MODEL", "gpt-4")  # or "gpt-4"
        self.temperature_value = self.get_temperature_value(chatbot.creativity)

    def get_temperature_value(self, creativity_level):
        if creativity_level == ChatbotCreativity.LOW:
            return 0.3
        elif creativity_level == ChatbotCreativity.MEDIUM:
            return 0.7
        elif creativity_level == ChatbotCreativity.HIGH:
            return 0.9
        else:
            return 0.7  # Default to medium