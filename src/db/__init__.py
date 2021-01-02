import os

from loguru import logger
from tinydb import TinyDB
from tinydb.storages import MemoryStorage

db_path = os.getenv("DB_PATH")
if db_path:
    logger.info(f'init db on path={db_path}')
    db = TinyDB(db_path)
else:
    logger.info('init in-memory db')
    db = TinyDB(storage=MemoryStorage)
