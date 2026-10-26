import argparse
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# Load local credentials and optional settings without overriding shell variables.
load_dotenv(Path(__file__).with_name(".env"))

DEFAULT_API_URL = "https://api.petpooja.com/V1/orders/get_sales_data/"
DEFAULT_FROM_DATE = "2025-05-03 00:00:00"
DEFAULT_TO_DATE = "2025-05-30 23:59:19"
DATABASE_PATH = Path(__file__).with_name("sales_data.db")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sales_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_number TEXT,
    sale_date TEXT,
    transaction_time TEXT,
    sale_amount REAL,
    tax_amount REAL,
    discount_amount REAL,
    round_off REAL,
    net_sale REAL,
    payment_mode TEXT,
    order_type TEXT,
    transaction_status TEXT
)
"""

# Keep insert order aligned with the table and the mapped row tuple.
SALES_COLUMNS = (
    "receipt_number",
    "sale_date",
    "transaction_time",
    "sale_amount",
    "tax_amount",
    "discount_amount",
    "round_off",
    "net_sale",
    "payment_mode",
    "order_type",
    "transaction_status",
)

FIELD_MAP = {
    "Receipt number": "receipt_number",
    "Receipt Date": "sale_date",
    "Receipt time": "transaction_time",
    "Invoice amount": "sale_amount",
    "Tax amount": "tax_amount",
    "Discount amount": "discount_amount",
    "Round off": "round_off",
    "Net sale": "net_sale",
    "Payment mode": "payment_mode",
    "Order type": "order_type",
    "Transaction status": "transaction_status",
}


def normalize_key(value: str) -> str:
    """Ignore case, spaces, and punctuation when matching API field names."""
    return "".join(character for character in value.lower() if character.isalnum())


def extract_records(payload: Any) -> list[dict[str, Any]]:
    """Find sales rows in a direct list or a nested API response envelope."""
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            return payload
        raise ValueError("The API returned a list containing non-object values")

    if not isinstance(payload, dict):
        raise ValueError("The API response must be a JSON object or list")

    status = payload.get("status", payload.get("success"))
    failed_status = status is False or (
        isinstance(status, str)
        and status.lower() in {"0", "false", "error"}
    )
    if failed_status:
        message = payload.get("message", "API reported an unsuccessful response")
        raise ValueError(str(message))

    # Prefer common response containers before searching other nested objects.
    preferred_keys = {"data", "salesdata", "orders", "result", "records"}
    for key, value in payload.items():
        if normalize_key(str(key)) in preferred_keys:
            try:
                return extract_records(value)
            except ValueError:
                pass

    for value in payload.values():
        if isinstance(value, dict):
            try:
                return extract_records(value)
            except ValueError:
                continue

    raise ValueError("Could not find a list of sales records in the API response")


def _number(value: Any) -> float | None:
    """Convert a numeric API value to float, allowing comma separators."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid numeric sales value: {value!r}") from error


def map_record(record: dict[str, Any]) -> tuple[Any, ...]:
    """Map one API record to values ordered for insertion into sales_data."""
    values = {normalize_key(str(key)): value for key, value in record.items()}
    mapped = {
        column: values.get(normalize_key(source_field))
        for source_field, column in FIELD_MAP.items()
    }

    numeric_columns = (
        "sale_amount",
        "tax_amount",
        "discount_amount",
        "round_off",
        "net_sale",
    )
    for column in numeric_columns:
        mapped[column] = _number(mapped[column])

    return tuple(mapped[column] for column in SALES_COLUMNS)


def create_session() -> requests.Session:
    """Create an HTTP session that retries temporary GET failures."""
    # Retry rate limits and common server errors, with a delay between attempts.
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_sales() -> list[dict[str, Any]]:
    """Request sales for the configured date range and parse the JSON rows."""
    required = (
        "PETPOOJA_APP_KEY",
        "PETPOOJA_APP_SECRET",
        "PETPOOJA_ACCESS_TOKEN",
        "PETPOOJA_REST_ID",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        # Stop before making a request if any required credential is unavailable.
        missing_names = ", ".join(missing)
        raise ValueError(f"Missing required environment variables: {missing_names}")

    # Keep credentials in environment variables instead of source code.
    params = {
        "app_key": os.environ["PETPOOJA_APP_KEY"],
        "app_secret": os.environ["PETPOOJA_APP_SECRET"],
        "access_token": os.environ["PETPOOJA_ACCESS_TOKEN"],
        "restID": os.environ["PETPOOJA_REST_ID"],
        "from_date": os.getenv("PETPOOJA_FROM_DATE", DEFAULT_FROM_DATE),
        "to_date": os.getenv("PETPOOJA_TO_DATE", DEFAULT_TO_DATE),
    }
    try:
        with create_session() as session:
            response = session.get(
                os.getenv("PETPOOJA_API_URL", DEFAULT_API_URL),
                params=params,
                timeout=(10, 60),
            )
        response.raise_for_status()
    except requests.RequestException as error:
        # Do not include the request URL, which contains credential query parameters.
        message = (
            f"API request failed ({type(error).__name__}); "
            "request details were suppressed"
        )
        raise ValueError(message) from None
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError as error:
        # Convert invalid response content into a clear sync error.
        raise ValueError("The API response was not valid JSON") from error
    # extract_records also rejects API envelopes that report failure.
    return extract_records(payload)


def store_sales(
    records: list[dict[str, Any]],
    database_path: Path = DATABASE_PATH,
) -> int:
    """Insert sales rows, skipping only rows identical in every mapped field."""
    columns = ", ".join(SALES_COLUMNS)
    placeholders = ", ".join("?" for _ in SALES_COLUMNS)
    duplicate_conditions = " AND ".join(
        f"existing.{column} IS ?" for column in SALES_COLUMNS
    )
    # SQLite's IS comparison treats NULL values as equal for exact-row matching.
    insert_sql = (
        f"INSERT INTO sales_data ({columns}) "
        f"SELECT {placeholders} WHERE NOT EXISTS "
        f"(SELECT 1 FROM sales_data AS existing WHERE {duplicate_conditions})"
    )
    rows = [map_record(record) for record in records]

    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute(CREATE_TABLE_SQL)
            # Remove the legacy index; duplicate checks compare every sales field.
            connection.execute("DROP INDEX IF EXISTS idx_sales_data_receipt_key")
            # Bind each row twice: once for insertion and once for duplicate lookup.
            cursor = connection.executemany(insert_sql, [row + row for row in rows])
    return cursor.rowcount


def main() -> int:
    """Run the initialization or fetch-and-store command-line flow."""
    parser = argparse.ArgumentParser(
        description="Fetch Petpooja sales and store them in SQLite."
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Create the SQLite database and table without calling the API.",
    )
    args = parser.parse_args()

    try:
        if args.init_db:
            with closing(sqlite3.connect(DATABASE_PATH)) as connection:
                with connection:
                    connection.execute(CREATE_TABLE_SQL)
                    # Keep initialized databases consistent with the import path.
                    connection.execute(
                        "DROP INDEX IF EXISTS idx_sales_data_receipt_key"
                    )
            print(f"Initialized database: {DATABASE_PATH}")
            return 0

        records = fetch_sales()
        inserted = store_sales(records)
        print(
            f"Fetched {len(records)} sales record(s); "
            f"inserted {inserted} into {DATABASE_PATH}."
        )
        return 0
    except (requests.RequestException, sqlite3.Error, ValueError) as error:
        print(
            f"Sales sync failed: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
