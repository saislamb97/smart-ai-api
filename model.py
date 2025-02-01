from dotenv import load_dotenv
import os
import logging
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, BigInteger, ForeignKey, DateTime, Table
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import relationship, sessionmaker, declarative_base
from sqlalchemy.dialects.mysql import ENUM as SQLEnum
from pydantic import BaseModel

from enums import (
    AutomationStatus,
    ChatbotCreativity,
    ChatbotMode,
    ChatbotStatus,
    CountryEnum,
    GenderTypeEnum,
    MessageDirection,
    MessageStatus,
    UploadStatus,
    UserRoleEnum,
    UserStatusEnum,
    templateType,
    toolType
)

# Load environment variables from the .env file
load_dotenv()

# Fetch MySQL configuration from environment variables with defaults
MYSQL_HOST = os.getenv('MYSQL_HOST', 'localhost')
MYSQL_PORT = os.getenv('MYSQL_PORT', '3306')
MYSQL_USER = os.getenv('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', 'root')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'smart-db')

# Construct the DATABASE_URL for async engine
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"mysql+aiomysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up SQLAlchemy Base and AsyncSession
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False
)

class UserModel(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    email = Column(String(100), unique=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    user_role = Column(SQLEnum(UserRoleEnum), nullable=False)
    user_status = Column(SQLEnum(UserStatusEnum), nullable=False)
    gender = Column(SQLEnum(GenderTypeEnum), nullable=False)


# Association Table
chatbots_files = Table(
    'chatbots_files', Base.metadata,
    Column('chatbot_model_id', BigInteger, ForeignKey('chatbots.id'), primary_key=True),
    Column('files_id', BigInteger, ForeignKey('files.id'), primary_key=True)
)
class FileModel(Base):
    __tablename__ = "files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    file_url = Column(String(255), nullable=False, unique=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship back to chatbots via association table
    chatbots = relationship("ChatbotModel", secondary=chatbots_files, back_populates="files")

class ChatModel(Base):
    __tablename__ = "chats"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    chatbot_id = Column(BigInteger, ForeignKey("chatbots.id"), nullable=False)
    email = Column(String(100), nullable=True)
    phone = Column(String(15), nullable=False)
    query = Column(Text, nullable=False)
    response = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    chatbot = relationship("ChatbotModel", back_populates="chats", lazy="select")

class ChatbotModel(Base):
    __tablename__ = "chatbots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    unique_id = Column(String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    status = Column(SQLEnum(ChatbotStatus), nullable=False)
    mode = Column(SQLEnum(ChatbotMode), nullable=False)
    creativity = Column(SQLEnum(ChatbotCreativity), nullable=False)
    word_count = Column(BigInteger, nullable=False, default=0)
    phone_id = Column(String(255))  # Optional phone ID
    version = Column(String(255))  # Optional version
    verify_token = Column(String(255))  # Optional verify token
    webhook = Column(String(255))  # Optional webhook URL
    access_token = Column(Text)  # Optional access token
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("UserModel", lazy="joined")  # Unidirectional relationship to UserModel
    chats = relationship("ChatModel", back_populates="chatbot", cascade="all, delete-orphan", lazy="subquery")
    # Relationship to files via association table
    files = relationship(
        "FileModel",
        secondary=chatbots_files,
        back_populates="chatbots"
    )

class WhatsappModel(Base):
    __tablename__ = "whatsapp"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    account_id = Column(String(250), nullable=False, unique=True)
    phone_id = Column(String(250), nullable=False)
    version = Column(String(250), nullable=False)
    verify_token = Column(String(36), nullable=False, default=lambda: str(uuid.uuid4()))
    webhook = Column(String(250), nullable=False)
    access_token = Column(Text, nullable=False)

    user = relationship("UserModel", lazy="joined")  # Unidirectional relationship to UserModel
    messages = relationship("WhatsappMessageModel", back_populates="whatsapp", cascade="all, delete-orphan", lazy="subquery")

class ContactModel(Base):
    __tablename__ = "contacts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    phone = Column(String(20), unique=True, nullable=False)
    email = Column(String(150), unique=True, nullable=False)
    country = Column(SQLEnum(CountryEnum), nullable=False)
    address = Column(Text, nullable=True)
    company = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Unidirectional relationship to UserModel
    user = relationship("UserModel", lazy="joined")

class WhatsappMessageModel(Base):
    __tablename__ = "whatsapp_messages"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    whatsapp_id = Column(BigInteger, ForeignKey("whatsapp.id"), nullable=False)
    contact_id = Column(BigInteger, ForeignKey("contacts.id"), nullable=False)
    direction = Column(SQLEnum(MessageDirection), nullable=False)
    message_content = Column(Text, nullable=False)
    media_id = Column(String, nullable=True)  # To store media ID if message has attachment
    media_type = Column(String, nullable=True)  # Type of media (image, document, etc.)
    caption = Column(String, nullable=True)  # Caption for media
    status = Column(SQLEnum(MessageStatus), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    whatsapp = relationship("WhatsappModel", lazy="joined")


# Models
class AutoFileModel(Base):
    __tablename__ = "auto_files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    automation_id = Column(BigInteger, ForeignKey("automation.id"), nullable=False)
    batch_name = Column(String(255), nullable=False)
    batch_id = Column(String(6), unique=True, nullable=False)
    upload_status = Column(SQLEnum(UploadStatus), nullable=False, default=UploadStatus.PENDING)
    encrypt_file = Column(String(255), unique=True, nullable=False)
    encrypt_key = Column(String(36), nullable=False)
    template_file = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    automation = relationship("AutomationModel", back_populates="batch_files")


class AutomationModel(Base):
    __tablename__ = "automation"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    account_id = Column(String(250), nullable=False, unique=True)
    phone_id = Column(String(250), nullable=False)
    version = Column(String(250), nullable=False)
    verify_token = Column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    webhook = Column(String(250), nullable=False, unique=True)
    access_token = Column(Text, nullable=False)
    email = Column(String(250), nullable=False)
    server = Column(String(250), nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String(250), nullable=False)
    password = Column(Text, nullable=False)
    status = Column(SQLEnum(AutomationStatus), nullable=False, default=AutomationStatus.ACTIVE)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    batch_files = relationship("AutoFileModel", back_populates="automation", cascade="all, delete-orphan")


# Pydantic models for request validation
class ChatbotRequest(BaseModel):
    chatbot_unique_id: str
    email: str
    phone: str
    message: str

class ChatHistoryRequest(BaseModel):
    chatbot_unique_id: str
    email: str
    phone: str

class SendMessageRequest(BaseModel):
    whatsappId: int
    contactId: int
    messageContent: str
    direction: MessageDirection

class AutoFileRequestModel(BaseModel):
    autoFileId: int
    automationId: int
    templateType: templateType
    toolType: toolType

# Dependency to get async DB session
async def get_db():
    async with AsyncSessionLocal() as db:
        try:
            yield db
        finally:
            await db.close()
