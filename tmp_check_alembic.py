import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
load_dotenv()
engine = create_engine(f"{os.getenv('DB_DRIVER','postgresql+psycopg2')}://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT','5432')}/{os.getenv('DB_NAME')}?sslmode={os.getenv('DB_SSLMODE','require')}")
with engine.connect() as conn:
    try:
        print(conn.execute(text('select version_num from alembic_version')).scalar())
    except Exception as e:
        print('ERROR', repr(e))
