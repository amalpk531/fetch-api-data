# Petpooja Sales Sync

Fetch sales from the Petpooja API and save them to `sales_data.db`.

## Setup

Requires Python 3.10 or newer.

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Add your Petpooja credentials to `.env`:

```dotenv
PETPOOJA_APP_KEY=your-app-key
PETPOOJA_APP_SECRET=your-app-secret
PETPOOJA_ACCESS_TOKEN=your-access-token
PETPOOJA_REST_ID=your-restaurant-id
```

## Run

Fetch and store sales:

```powershell
python sales_sync.py
```

Create the database without fetching data:

```powershell
python sales_sync.py --init-db
```

The date range can be changed with `PETPOOJA_FROM_DATE` and `PETPOOJA_TO_DATE` in `.env`. Keep real credentials in `.env`; do not commit them.

The script skips rows that exactly match existing sales data. A message such as `inserted 0` means the fetched records were already stored.

## Error handling

Temporary API failures and rate limits are retried up to four times. If the request still fails, the response is not valid JSON, or the API reports an unsuccessful result, the script prints an error and exits with a non-zero status. Request details are suppressed to avoid exposing credentials.

