import os
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import asyncpg
import jwt
import trafilatura
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from passlib.context import CryptContext
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

JWT_SECRET = os.getenv("JWT_SECRET", "change-this-in-production")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

pool: asyncpg.Pool = None

mistral_client = OpenAI(
    api_key=os.getenv("MISTRAL_API_KEY"),
    base_url="https://api.mistral.ai/v1"
)



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
        # Создаём таблицу users
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        # Создаём таблицу articles
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                title TEXT,
                content TEXT,
                summary TEXT,
                tags TEXT[],
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_id, url)
            );
        """)
        
        # Миграция: если колонки user_id нет — добавляем
        column_check = await conn.fetch("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='articles' AND column_name='user_id'
        """)
        if not column_check:
            await conn.execute("""
                ALTER TABLE articles ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE
            """)
            # Удаляем старый уникальный констрейнт (если есть)
            await conn.execute("""
                ALTER TABLE articles DROP CONSTRAINT IF EXISTS articles_url_key
            """)
            # Добавляем новый уникальный констрейнт
            await conn.execute("""
                ALTER TABLE articles ADD CONSTRAINT articles_user_id_url_key UNIQUE (user_id, url)
            """)
    yield
    await pool.close()

app = FastAPI(title="Smart Reader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== Модели =====

class ArticleIn(BaseModel):
    url: HttpUrl


class ArticleOut(BaseModel):
    id: int
    url: str
    title: Optional[str]
    summary: Optional[str]
    tags: list[str]
    created_at: datetime


class UserRegister(BaseModel):
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


# ===== Хелперы =====

def create_token(user_id: int) -> str:
    return jwt.encode({"user_id": user_id}, JWT_SECRET, algorithm="HS256")


def get_current_user(authorization: str = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return int(payload["user_id"])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def extract_article(url: str) -> dict:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise HTTPException(status_code=422, detail="Cannot fetch URL")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
    metadata = trafilatura.extract(downloaded, include_formatting=False, with_metadata=True)
    title = ""
    if metadata:
        lines = metadata.split("\n")
        if lines:
            title = lines[0][:200]
    return {
        "content": text or "",
        "title": title,
    }


def generate_summary(text: str) -> dict:
    text = text[:10000]
    prompt = f"""Проанализируй следующий текст и верни JSON с двумя полями:
1. "summary" - краткая выжимка в виде ОДНОЙ СТРОКИ с пунктами, разделёнными переносами строки
2. "tags" - массив из 3-5 релевантных тегов (короткие слова на русском)

Текст:
{text}

Верни ТОЛЬКО валидный JSON, без пояснений, без markdown."""

    try:
        response = mistral_client.chat.completions.create(
            model="mistral-small-latest",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]

        result = json.loads(content)

        summary = result.get("summary", "")
        if isinstance(summary, list):
            summary = "\n".join(str(item) for item in summary)

        tags = result.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]

        return {"summary": str(summary), "tags": tags if isinstance(tags, list) else []}
    except Exception as e:
        print(f"Mistral error: {e}")
        return {"summary": "Ошибка генерации саммари", "tags": []}


# ===== Эндпоинты =====

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/register", response_model=Token)
async def register(user: UserRegister):
    if len(user.username) < 2:
        raise HTTPException(status_code=400, detail="Username too short")
    if len(user.password) < 4:
        raise HTTPException(status_code=400, detail="Password too short")

    password_hash = pwd_context.hash(user.password)
    try:
        row = await pool.fetchrow(
            "INSERT INTO users (username, password_hash) VALUES ($1, $2) RETURNING id",
            user.username, password_hash
        )
        return {"access_token": create_token(row["id"]), "token_type": "bearer"}
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=400, detail="Username already exists")


@app.post("/login", response_model=Token)
async def login(user: UserLogin):
    row = await pool.fetchrow(
        "SELECT id, password_hash FROM users WHERE username = $1",
        user.username
    )
    if not row or not pwd_context.verify(user.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": create_token(row["id"]), "token_type": "bearer"}


@app.post("/auto-register", response_model=Token)
async def auto_register(user: UserRegister):
    if len(user.username) < 2:
        raise HTTPException(status_code=400, detail="Имя слишком короткое")
    
    import secrets
    random_password = secrets.token_urlsafe(32)
    password_hash = pwd_context.hash(random_password)
    
    username = user.username
    suffix = 1
    while True:
        try:
            row = await pool.fetchrow(
                "INSERT INTO users (username, password_hash) VALUES ($1, $2) RETURNING id",
                username, password_hash
            )
            break
        except asyncpg.UniqueViolationError:
            username = f"{user.username}{suffix}"
            suffix += 1
            if suffix > 100:
                raise HTTPException(status_code=400, detail="Попробуй другое имя")
    
    return {"access_token": create_token(row["id"]), "token_type": "bearer"}

@app.post("/articles", response_model=ArticleOut)
async def add_article(payload: ArticleIn, user_id: int = Depends(get_current_user)):
    url = str(payload.url)
    data = extract_article(url)
    if not data["content"].strip():
        raise HTTPException(status_code=422, detail="Empty article content")

    ai_result = generate_summary(data["content"])
    summary = ai_result.get("summary", "")
    tags = ai_result.get("tags", [])

    row = await pool.fetchrow(
        """
        INSERT INTO articles (user_id, url, title, content, summary, tags)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (user_id, url) DO UPDATE SET 
            title = EXCLUDED.title, 
            content = EXCLUDED.content,
            summary = EXCLUDED.summary,
            tags = EXCLUDED.tags
        RETURNING id, url, title, summary, tags, created_at
        """,
        user_id, url, data["title"], data["content"], summary, tags,
    )
    return dict(row)


@app.get("/articles", response_model=list[ArticleOut])
async def list_articles(limit: int = 50, user_id: int = Depends(get_current_user)):
    rows = await pool.fetch(
        "SELECT id, url, title, summary, tags, created_at FROM articles WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
        user_id, limit,
    )
    return [dict(r) for r in rows]


@app.delete("/articles/{article_id}")
async def delete_article(article_id: int, user_id: int = Depends(get_current_user)):
    res = await pool.execute(
        "DELETE FROM articles WHERE id = $1 AND user_id = $2",
        article_id, user_id
    )
    if res == "DELETE 0":
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}