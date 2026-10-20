import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
con = sqlite3.connect('file:storage/sessions.db?mode=ro', uri=True)
q = "SELECT id,status,started_at,finished_at FROM sessions WHERE status IN ('running','paused','waiting') ORDER BY id"
rows = con.execute(q).fetchall()
print(f"  中间态会话 {len(rows)} 个:")
for r in rows:
    n = con.execute("SELECT COUNT(*) FROM events WHERE session_id=?", (r[0],)).fetchone()[0]
    print(f"    #{r[0]:<3} {r[1]:<8} start={r[2]}  events={n}")
con.close()
