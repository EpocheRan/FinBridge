import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.init_market_data_db import init_market_tables
from scripts.init_position_db import init_tables as init_position_tables
from utils.DataConnect import questDB

# Setup logging
logger = logging.getLogger("DBInitializer")


def check_and_initialize_tables():
    """
    Check if tables exist, if not, create them.
    This calls initialization functions for both market data and position data.
    """
    logger.info("Checking and initializing all database tables...")

    try:
        db = questDB()
        if not db.is_connected():
            logger.error("Failed to connect to QuestDB. Cannot initialize tables.")
            return

        # Initialize Market Data Tables
        # This function handles "CREATE TABLE IF NOT EXISTS"
        init_market_tables(db)

        # Initialize Position Tables
        # This function also handles "CREATE TABLE IF NOT EXISTS"
        init_position_tables()

        logger.info("All tables check/creation complete.")

    except Exception as e:
        logger.error(f"Error during table initialization: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    check_and_initialize_tables()
