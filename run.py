#!/usr/bin/env python3
"""
BatterySitter runner script that loads configuration from config.json
"""

import asyncio
import logging
import sys
import os
import json

from battery_sitter import BatterySitter


def load_config(config_path='config.json'):
    """Load configuration from JSON file"""
    if not os.path.exists(config_path):
        print(f"ERROR: {config_path} not found!")
        print("Please copy config.example.json to config.json and fill in your credentials.")
        sys.exit(1)

    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {config_path}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Failed to load {config_path}: {e}")
        sys.exit(1)


async def main():
    """Main entry point with configuration loaded from config.json"""
    # Load configuration
    config = load_config()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('battery_sitter.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Suppress verbose HTTP logs from httpx
    logging.getLogger('httpx').setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("BatterySitter starting...")
    logger.info("=" * 60)

    # Create BatterySitter instance with config from JSON
    sitter = BatterySitter(
        zappi_username=config['zappi']['username'],
        zappi_password=config['zappi']['password'],
        zappi_serial=config['zappi']['serial'],
        sigenergy_username=config['sigenergy']['username'],
        sigenergy_password=config['sigenergy']['password'],
        sigenergy_region=config['sigenergy']['region'],
        poll_interval=config['polling']['interval_seconds'],
        charging_power=config['sigenergy']['charging_power']
    )

    # Run the service
    await sitter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
