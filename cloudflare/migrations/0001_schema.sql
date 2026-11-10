PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS word_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    meaning TEXT NOT NULL,
    is_mastered INTEGER DEFAULT 0,
    is_error INTEGER DEFAULT 0,
    stability REAL DEFAULT 0,
    difficulty REAL DEFAULT 0,
    reps INTEGER DEFAULT 0,
    lapses INTEGER DEFAULT 0,
    next_review_at DATETIME NULL,
    last_review_at DATETIME NULL,
    FOREIGN KEY (book_id) REFERENCES word_books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id INTEGER NOT NULL,
    review_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    score INTEGER,
    is_correct INTEGER,
    FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_details (
    word_id INTEGER PRIMARY KEY,
    phrases TEXT,
    examples TEXT,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    pos TEXT,
    FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_writings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    style TEXT NOT NULL,
    target_len INTEGER,
    content TEXT NOT NULL,
    word_list TEXT,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES word_books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS study_sessions (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_jobs (
    id TEXT PRIMARY KEY,
    book_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    target_ids TEXT NOT NULL,
    done INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT DEFAULT '',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES word_books(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_words_book ON words(book_id);
CREATE INDEX IF NOT EXISTS idx_words_due ON words(next_review_at);
CREATE INDEX IF NOT EXISTS idx_logs_word_time ON review_logs(word_id, review_time);
CREATE INDEX IF NOT EXISTS idx_logs_time ON review_logs(review_time);
CREATE UNIQUE INDEX IF NOT EXISTS uq_words_book_word ON words(book_id, word);
CREATE INDEX IF NOT EXISTS idx_writings_book_day ON daily_writings(book_id, day);
CREATE INDEX IF NOT EXISTS idx_study_sessions_expiry ON study_sessions(expires_at);
