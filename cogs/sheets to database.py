import os
import asyncio
import aiohttp
import asyncpg
import csv

DATABASE_DSN = "postgresql://postgres.nfqgryxakejkoiqxeboo:Thddngur1005%21@aws-0-ca-central-1.pooler.supabase.com:6543/postgres?sslmode=require"
CSV_URL = "https://docs.google.com/spreadsheets/d/1SINW2NVOQJzpNPk1xMSUkbb9mjGDCixamzaCETfnCIo/export?format=csv"
HENRIK_API_KEY = "HDEV-9985b541-1180-4deb-9a98-79535d298ab9"

CREATE_PLAYER_SQL = """
INSERT INTO players (discord_id, puuid, riot_name, riot_tag, seeded, last_active, created_at)
VALUES ($1, $2, $3, $4, TRUE, NOW(), NOW())
ON CONFLICT (discord_id) DO UPDATE SET
    puuid = EXCLUDED.puuid,
    riot_name = EXCLUDED.riot_name,
    riot_tag = EXCLUDED.riot_tag,
    seeded = TRUE,
    last_active = NOW();
"""

async def fetch_puuid(riot_name, riot_tag):
    # Uses Henrik API to fetch puuid if needed
    url = f"https://api.henrikdev.xyz/valorant/v2/account/{riot_name}/{riot_tag}"
    headers = {"Authorization": HENRIK_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                print(f"❌ Henrik API error for {riot_name}#{riot_tag}: {resp.status}")
                return None
            data = await resp.json()
            return data.get("data", {}).get("puuid")

async def seed_from_sheet():
    # Download CSV
    async with aiohttp.ClientSession() as session:
        async with session.get(CSV_URL) as resp:
            csv_text = await resp.text()
    print("CSV downloaded, length:", len(csv_text))

    rows = list(csv.DictReader(csv_text.splitlines()))
    print(f"Parsed {len(rows)} rows.")
    if not rows:
        print("❌ No data found in CSV. Check sharing and format.")
        return

    pool = await asyncpg.create_pool(DATABASE_DSN, statement_cache_size=0)
    count, skipped = 0, 0

    for row in rows:
        # Defensive: skip blank rows
        if not row or not row.get('USER ID'):
            continue
        discord_id = row.get('USER ID', '').strip()
        riot_name = row.get('RIOT NAME', '').strip()
        riot_tag = row.get('RIOT TAG', '').strip()
        puuid = row.get('PUUID', '').strip() if 'PUUID' in row else ''

        if not discord_id or not discord_id.isdigit():
            print(f"❌ WARNING: USER ID not a numeric Discord user ID: {discord_id}")
            skipped += 1
            continue
        if not riot_name or not riot_tag:
            print(f"⚠️ Skipping row with missing RIOT NAME/TAG: {row}")
            skipped += 1
            continue

        if not puuid:
            puuid = await fetch_puuid(riot_name, riot_tag)
            if not puuid:
                print(f"⚠️ Skipping {riot_name}#{riot_tag} (USER ID: {discord_id}) - PUUID not found.")
                skipped += 1
                continue

        async with pool.acquire() as conn:
            await conn.execute(
                CREATE_PLAYER_SQL,
                str(discord_id), puuid, riot_name, riot_tag
            )
            print(f"✅ Seeded {riot_name}#{riot_tag} (USER ID: {discord_id}, PUUID: {puuid})")
        count += 1

        # --- Insert a 10 second delay between each operation ---
        await asyncio.sleep(10)

    await pool.close()
    print(f"\n✅ Done! Seeded {count} users, skipped {skipped}.")

if __name__ == "__main__":
    asyncio.run(seed_from_sheet())
