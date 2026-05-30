#!/usr/bin/env python3
"""
Dev Deck Memory Store
A zero-dependency, ultra-fast codebase search tool using SQLite FTS5.
"""
import argparse
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / ".hermes" / "memory.db"

IGNORE_DIRS = {".git", "node_modules", "__pycache__", "venv", "env", ".hermes", "logs", "dist", "build"}
IGNORE_EXTS = {".exe", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".pyc", ".db"}

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS codebase USING fts5(path, content);")
    return conn

def index_dir(directory: str):
    root = Path(directory).resolve()
    print(f"Indexing {root}...")
    conn = get_db()
    
    # Clear existing index for this directory to prevent duplicates
    # Since FTS doesn't support UPDATE easily with LIKE, we just clear and rebuild.
    conn.execute("DELETE FROM codebase;")
    
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        
        for file in filenames:
            if any(file.endswith(ext) for ext in IGNORE_EXTS):
                continue
                
            filepath = Path(dirpath) / file
            try:
                content = filepath.read_text(encoding="utf-8")
                rel_path = str(filepath.relative_to(root)).replace("\\", "/")
                conn.execute("INSERT INTO codebase (path, content) VALUES (?, ?)", (rel_path, content))
                count += 1
            except Exception:
                pass # Skip binary or unreadable files
                
    conn.commit()
    conn.close()
    print(f"✅ Indexed {count} files securely in SQLite FTS5.")

def search(query: str, limit: int = 5):
    conn = get_db()
    # Snippet function extracts context around the match
    sql = """
        SELECT path, snippet(codebase, 1, '[[MATCH]]', '[[/MATCH]]', '...', 20) as context 
        FROM codebase 
        WHERE codebase MATCH ? 
        ORDER BY rank 
        LIMIT ?
    """
    try:
        # FTS5 requires quotes around terms for exact matching if special chars exist
        # We try raw query first, if it fails due to syntax, we wrap it.
        try:
            cursor = conn.execute(sql, (query, limit))
        except sqlite3.OperationalError:
            cursor = conn.execute(sql, (f'"{query}"', limit))
            
        results = cursor.fetchall()
        if not results:
            print("No matches found.")
            return
            
        print(f"Found {len(results)} matches for '{query}':\n")
        for path, context in results:
            print(f"--- {path} ---")
            print(f"{context}\n")
            
    except Exception as e:
        print(f"Search error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dev Deck Memory Store (FTS5)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    idx_parser = subparsers.add_parser("index", help="Index a directory")
    idx_parser.add_argument("dir", type=str, default=".", nargs="?")
    
    srch_parser = subparsers.add_parser("search", help="Search the codebase")
    srch_parser.add_argument("query", type=str)
    srch_parser.add_argument("--limit", type=int, default=5)
    
    args = parser.parse_args()
    
    if args.command == "index":
        index_dir(args.dir)
    elif args.command == "search":
        search(args.query, args.limit)
