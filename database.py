import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# Database configuration - Supabase PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL',
    "postgresql://postgres.vjymiljkemfwbcccxfqb:Ihaveadatabase!@aws-1-eu-west-1.pooler.supabase.com:5432/postgres"
)

def get_db_connection():
    """Create and return a database connection"""
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    return conn

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Initialize database tables"""
    conn = get_db_connection()
    cur = conn.cursor()

    # Create users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(20) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            remember_token VARCHAR(64) UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Add remember_token column if it doesn't exist (for existing DBs)
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS remember_token VARCHAR(64) UNIQUE;
    """)

    # Create tokens table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id SERIAL PRIMARY KEY,
            creator_id INTEGER REFERENCES users(id),
            recipient_id INTEGER REFERENCES users(id),
            name VARCHAR(50) NOT NULL,
            description TEXT,
            duration_minutes INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_at TIMESTAMP,
            status VARCHAR(20) DEFAULT 'available' CHECK (status IN ('available', 'in_progress', 'completed'))
        )
    """)

    # Create shopping lists table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shopping_lists (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            created_by INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)

    # Create shopping items table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shopping_items (
            id SERIAL PRIMARY KEY,
            list_id INTEGER REFERENCES shopping_lists(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            quantity VARCHAR(20) DEFAULT '1',
            category VARCHAR(50) DEFAULT 'pcs',
            is_completed BOOLEAN DEFAULT FALSE,
            added_by INTEGER REFERENCES users(id),
            completed_by INTEGER REFERENCES users(id),
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    """)

    # Create shopping list members table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shopping_list_members (
            id SERIAL PRIMARY KEY,
            list_id INTEGER REFERENCES shopping_lists(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id),
            role VARCHAR(10) DEFAULT 'member' CHECK (role IN ('owner', 'member')),
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(list_id, user_id)
        )
    """)

    # Create scratch prizes table (prize list per user, configured by admin)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scratch_prizes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            token_name VARCHAR(50),
            token_description TEXT,
            token_duration_minutes INTEGER DEFAULT 30,
            probability FLOAT NOT NULL,
            is_loser BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create scratch tickets table (one ticket per user per day)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scratch_tickets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            ticket_date DATE NOT NULL,
            scratched_at TIMESTAMP,
            prize_id INTEGER REFERENCES scratch_prizes(id),
            UNIQUE(user_id, ticket_date)
        )
    """)

    # Create wheel countries table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wheel_countries (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            flag_emoji VARCHAR(10) NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── Flashcard Leitner tables ─────────────────────────────────────────────

    # Languages
    cur.execute("""
        CREATE TABLE IF NOT EXISTS languages (
            id SERIAL PRIMARY KEY,
            code VARCHAR(10) UNIQUE NOT NULL,
            name VARCHAR(50) NOT NULL,
            flag_emoji VARCHAR(10)
        )
    """)

    # Flashcard categories
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flashcard_categories (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL UNIQUE,
            description TEXT,
            icon VARCHAR(10)
        )
    """)
    # Add unique constraint if missing (for existing DBs)
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE flashcard_categories ADD CONSTRAINT flashcard_categories_name_key UNIQUE (name);
        EXCEPTION WHEN duplicate_table THEN NULL;
        END $$;
    """)

    # Flashcards (global pool)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flashcards (
            id SERIAL PRIMARY KEY,
            category_id INTEGER REFERENCES flashcard_categories(id) ON DELETE CASCADE,
            translations JSONB NOT NULL,
            audio_hint TEXT,
            difficulty VARCHAR(10) DEFAULT 'medium' CHECK (difficulty IN ('beginner', 'medium', 'confirmed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Add difficulty column if it doesn't exist (for existing DBs)
    cur.execute("""
        ALTER TABLE flashcards ADD COLUMN IF NOT EXISTS
        difficulty VARCHAR(10) DEFAULT 'medium' CHECK (difficulty IN ('beginner', 'medium', 'confirmed'));
    """)

    # Flashcard distractors (wrong answers for QCM)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flashcard_distractors (
            id SERIAL PRIMARY KEY,
            flashcard_id INTEGER REFERENCES flashcards(id) ON DELETE CASCADE,
            language_code VARCHAR(10) NOT NULL,
            distractor_text VARCHAR(200) NOT NULL
        )
    """)

    # User flashcards (personal collection + Leitner state)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_flashcards (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            flashcard_id INTEGER REFERENCES flashcards(id) ON DELETE CASCADE,
            source_lang VARCHAR(10) NOT NULL,
            target_lang VARCHAR(10) NOT NULL,
            leitner_box INTEGER DEFAULT 1 CHECK (leitner_box BETWEEN 1 AND 7),
            next_review_date DATE DEFAULT CURRENT_DATE,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Leitner intervals (admin-configurable)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leitner_intervals (
            box_number INTEGER PRIMARY KEY CHECK (box_number BETWEEN 1 AND 7),
            days_interval INTEGER NOT NULL CHECK (days_interval > 0)
        )
    """)

    # Seed default Leitner intervals if empty
    cur.execute("SELECT COUNT(*) as cnt FROM leitner_intervals")
    if cur.fetchone()['cnt'] == 0:
        for box, days in [(1,1),(2,2),(3,4),(4,7),(5,14),(6,30),(7,90)]:
            cur.execute(
                "INSERT INTO leitner_intervals (box_number, days_interval) VALUES (%s, %s)",
                (box, days)
            )

    conn.commit()
    cur.close()
    conn.close()

    print("Database initialized successfully! (Supabase)")
