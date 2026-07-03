import psycopg

DB_CONNECTION_STRING = "dbname=report_form_engine user=postgres password=@Frank254 host=localhost port=5432"

try:
    print("Connecting to PostgreSQL database...")
    with psycopg.connect(DB_CONNECTION_STRING) as conn:
        with conn.cursor() as cur:
            print("Dropping old table architectures...")
            cur.execute("DROP TABLE IF EXISTS student_scores CASCADE;")
            cur.execute("DROP TABLE IF EXISTS learning_areas CASCADE;")
            conn.commit()
            print("🚀 Success! Old conflicting constraint tables have been purged.")
except Exception as e:
    print(f"❌ Error executing script: {e}")