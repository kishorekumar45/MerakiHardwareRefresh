import truststore

truststore.inject_into_ssl()

import os

import meraki
from dotenv import load_dotenv


load_dotenv()

api_key = os.getenv("MERAKI_API_KEY")

if not api_key:
    raise SystemExit("MERAKI_API_KEY was not found in .env")

dashboard = meraki.DashboardAPI(
    api_key,
    suppress_logging=True,
    print_console=False,
    wait_on_rate_limit=True,
    maximum_retries=2,
)

organizations = dashboard.organizations.getOrganizations()

print(f"Connection successful. Accessible organizations: {len(organizations)}")

for organization in organizations:
    print(f"- {organization['name']} ({organization['id']})")