"""Unlink the project's billing account when the budget's net cost reaches its amount.

Triggered by the budget's Pub/Sub notifications (docs/gcp-migration.md 5.4b).
"""
import base64
import json
import os

import functions_framework
from googleapiclient import discovery

PROJECT = f"projects/{os.environ['PROJECT_ID']}"


@functions_framework.cloud_event
def stop_billing(cloud_event):
    msg = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    cost, budget = msg["costAmount"], msg["budgetAmount"]
    print(f"budget update: cost={cost} budget={budget}")
    if cost < budget:
        return
    # No getBillingInfo check first: Project Billing Manager may unlink billing but not read the
    # project (403 on 2026-11-15). Unlinking an already unlinked project is harmless.
    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    billing.projects().updateBillingInfo(name=PROJECT, body={"billingAccountName": ""}).execute()
    print(f"billing disabled for {PROJECT}: cost {cost} >= cap {budget}")
