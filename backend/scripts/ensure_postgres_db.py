import os
import time
import psycopg2
from psycopg2 import sql
from psycopg2.errors import DuplicateDatabase

host = os.environ.get('POSTGRES_HOST', 'postgres')
port = int(os.environ.get('POSTGRES_PORT', '5432'))
user = os.environ.get('POSTGRES_USER', 'postgres')
password = os.environ.get('POSTGRES_PASSWORD', 'postgres')
dbname = os.environ.get('POSTGRES_DB', 'touming_dev')

last_error = None
for _ in range(30):
    try:
        conn = psycopg2.connect(host=host, port=port, dbname='postgres', user=user, password=password, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM pg_database WHERE datname=%s', (dbname,))
            if not cur.fetchone():
                try:
                    cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(dbname)))
                except DuplicateDatabase:
                    pass
        conn.close()
        print(f'ensured database: {dbname}')
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(2)

raise SystemExit(f'failed to ensure database {dbname}: {last_error}')
