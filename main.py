import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import asyncpg
import trafilatura
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

pool: asyncpg.Pool = None  # type: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                content TEXT,
                summary TEXT,
                tags TEXT[],
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    yield
    await pool.close()


app = FastAPI(title="Smart Reader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # на старте — звёздочка, потом заменим на свой домен
    allow_methods=["*"],
    allow_headers=["*"],
)


class ArticleIn(BaseModel):
    url: HttpUrl


class ArticleOut(BaseModel):
    id: int
    url: str
    title: Optional[str]
    summary: Optional[str]
    tags: list[str]
    created_at: datetime


def extract_article(url: str) -> dict:
    """Скачивает страницу и вытаскивает чистый текст + заголовок."""
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise HTTPException(status_code=422, detail="Cannot fetch URL")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    title = trafilatura.extract(downloaded, output_format="xmltei")
    # Простой заголовок через metadata:
    metadata = trafilatura.extract(downloaded, include_formatting=False, with_metadata=True)
    return {
        "content": text or "",
        "title": (metadata.split("\n")[0] if metadata else "")[:200],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/articles", response_model=ArticleOut)
async def add_article(payload: ArticleIn):
    url = str(payload.url)

    # 1. Парсим
    data = extract_article(url)
    if not data["content"].strip():
        raise HTTPException(status_code=422, detail="Empty article content")

    # 2. Сохраняем (summary пока пустой — добавим ИИ на следующем шаге)
    row = await pool.fetchrow(
        """
        INSERT INTO articles (url, title, content, summary, tags)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title, content = EXCLUDED.content
        RETURNING id, url, title, summary, tags, created_at
        """,
        url,
        data["title"],
        data["content"],
        None,
        [],
    )
    return dict(row)


@app.get("/articles", response_model=list[ArticleOut])
async def list_articles(limit: int = 50):
    rows = await pool.fetch(
        "SELECT id, url, title, summary, tags, created_at FROM articles ORDER BY created_at DESC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


@app.delete("/articles/{article_id}")
async def delete_article(article_id: int):
    res = await pool.execute("DELETE FROM articles WHERE id = $1", article_id)
    if res == "DELETE 0":
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}