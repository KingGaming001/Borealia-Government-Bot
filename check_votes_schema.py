import sqlite3
import config

conn = sqlite3.connect(config.DATABASE_PATH)
cur = conn.cursor()

cur.execute("PRAGMA table_info(votes)")
cols = cur.fetchall()

print("DATABASE_PATH:", config.DATABASE_PATH)
print("votes columns:")
for c in cols:
    print("-", c[1], c[2])

conn.close()
