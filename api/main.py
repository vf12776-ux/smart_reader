import os
import json
import requests
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

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

HF_API_KEY = os.getenv("HF_API_KEY")
HF_MODEL = "mistralai/Mistral-7B-Instruct-v0.2"
HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    parsed = urlparse(DATABASE_URL)
    pool = await asyncpg.create_pool(
        user=parsed.username,
        password=parsed.password,
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        ssl="require",
        min_size=1,
        max_size=5,
    )
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
    allow_origins=["*"],
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
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise HTTPException(status_code=422, detail="Cannot fetch URL")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    metadata = trafilatura.extract(downloaded, include_formatting=False, with_metadata=True)
    return {
        "content": text or "",
        "title": (metadata.split("\n")[0] if metadata else "")[:200],
    }


def generate_summary(text: str) -> dict:
    text = text[:8000]
    prompt = f"""<s>[INST] Проанализируй следующий текст и верни JSON с двумя полями:
1. "summary" - краткая выжимка на 3-5 пунктов (каждый пункт с новой строки)
2. "tags" - массив из 3 релевантных тегов (короткие слова на русском)

Текст:
{text}

Верни ТОЛЬКО JSON, без пояснений. [/INST]"""

    try:
        response = requests.post(
            HF_API_URL,
            headers={
                "Authorization": f"Bearer {HF_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "inputs": prompt,
                "parameters": {"max_new_tokens": 500, "return_full_text": False}
            },
            timeout=30
        )
        
        if response.status_code != 200:
            print(f"HF error: {response.status_code} - {response.text}")
            return {"summary": "Ошибка генерации саммари", "tags": []}
        
        result = response.json()
        generated_text = result[0]["generated_text"].strip()
        
        # Убираем markdown-обёртку, если есть
        if generated_text.startswith("```"):
            generated_text = generated_text.split("\n", 1)[1].rsplit("```", 1)[0]
        
        return json.loads(generated_text)
    except Exception as e:
        print(f"HF error: {e}")
        return {"summary": "Ошибка генерации саммари", "tags": []}


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/articles", response_model=ArticleOut)
async def add_article(payload: ArticleIn):
    url = str(payload.url)
    data = extract_article(url)
    if not data["content"].strip():
        raise HTTPException(status_code=422, detail="Empty article content")

    ai_result = generate_summary(data["content"])
    summary = ai_result.get("summary", "")
    tags = ai_result.get("tags", [])

    row = await pool.fetchrow(
        """
        INSERT INTO articles (url, title, content, summary, tags)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (url) DO UPDATE SET 
            title = EXCLUDED.title, 
            content = EXCLUDED.content,
            summary = EXCLUDED.summary,
            tags = EXCLUDED.tags
        RETURNING id, url, title, summary, tags, created_at
        """,
        url,
        data["title"],
        data["content"],
        summary,
        tags,
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