import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from backend.database.models import Base
from backend.database.db import DATABASE_URL

async def main():
    print(f"Initializing database at: {DATABASE_URL}")
    engine = create_async_engine(DATABASE_URL, echo=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created successfully!")

if __name__ == "__main__":
    asyncio.run(main())
