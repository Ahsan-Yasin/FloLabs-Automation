"""Business logic of the product layer (accounts, sessions, API keys, email,
jobs index, webhooks). No FastAPI imports here: routes in api/ call these,
and so do the CLI (db/cli.py) and the job worker."""
