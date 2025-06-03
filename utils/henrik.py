import aiohttp
import os

HENRIK_API_KEY = os.getenv("HENRIK_API_KEY")

async def henrik_get(endpoint: str) -> dict:
    base = "https://api.henrikdev.xyz"
    headers = {"Authorization": HENRIK_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(base + endpoint, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                return None
