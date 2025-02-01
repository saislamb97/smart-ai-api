# redis_client.py

import os
import redis.asyncio as redis
from fastapi import FastAPI, Request
import logging

logger = logging.getLogger(__name__)

class RedisClient:
    def __init__(self):
        self.redis = None

    async def connect(self):
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        try:
            self.redis = redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
            # Test the connection
            await self.redis.ping()
            logger.info("Connected to Redis successfully.")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")

    async def disconnect(self):
        if self.redis:
            await self.redis.close()
            logger.info("Disconnected from Redis.")

redis_client = RedisClient()

# Dependency to provide Redis connection
async def get_redis_client():
    if not redis_client.redis:
        await redis_client.connect()
    return redis_client.redis
