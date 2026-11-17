# Kill switch: when the project's net cost after all credits reaches var.card_cap_usd,
# a Cloud Run function unlinks the billing account, which stops every paid service in the project.
# Budget data lags by hours, so this is a near cap, not an exact one. See docs/gcp-migration.md 5.4b.

data "google_project" "this" {}

resource "google_project_service" "killswitch_apis" {
  for_each = toset([
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
    "pubsub.googleapis.com",
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "eventarc.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

resource "google_pubsub_topic" "budget" {
  name       = "budget-alerts"
  depends_on = [google_project_service.killswitch_apis]
}

resource "google_billing_budget" "card_cap" {
  billing_account = var.billing_account
  display_name    = "agentic-16s card cap"
  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS" # net cost: what would be charged to the card
  }
  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.card_cap_usd)
    }
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  all_updates_rule {
    pubsub_topic                   = google_pubsub_topic.budget.id
    disable_default_iam_recipients = false # billing admins still get the emails
  }
  depends_on = [google_project_service.killswitch_apis]
}

# Runtime identity of the function. Project Billing Manager on this project only:
# it can unlink billing here and nowhere else.
resource "google_service_account" "killswitch" {
  account_id   = "budget-killswitch"
  display_name = "Unlinks billing when the card cap is hit"
  depends_on   = [google_project_service.killswitch_apis]
}

resource "google_project_iam_member" "killswitch_billing" {
  project = var.project_id
  role    = "roles/billing.projectManager"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}

# Eventarc calls the function as this service account
resource "google_project_iam_member" "killswitch_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}

# Dedicated build identity, so the build does not depend on the default compute service account
resource "google_service_account" "fn_build" {
  account_id   = "fn-build"
  display_name = "Builds Cloud Run functions"
  depends_on   = [google_project_service.killswitch_apis]
}

resource "google_project_iam_member" "fn_build" {
  for_each = toset([
    "roles/cloudbuild.builds.builder",
    "roles/logging.logWriter",
    "roles/artifactregistry.writer",
    "roles/storage.objectViewer",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.fn_build.email}"
}

data "archive_file" "killswitch" {
  type        = "zip"
  source_dir  = "${path.module}/killswitch"
  output_path = "${path.module}/.build/killswitch.zip"
}

resource "google_storage_bucket" "fn_source" {
  name                        = "${var.project_id}-fn-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  depends_on                  = [google_project_service.killswitch_apis]
}

resource "google_storage_bucket_object" "killswitch" {
  name   = "killswitch-${data.archive_file.killswitch.output_md5}.zip"
  bucket = google_storage_bucket.fn_source.name
  source = data.archive_file.killswitch.output_path
}

resource "google_cloudfunctions2_function" "killswitch" {
  name     = "budget-killswitch"
  location = var.region

  build_config {
    runtime         = "python312"
    entry_point     = "stop_billing"
    service_account = google_service_account.fn_build.id
    source {
      storage_source {
        bucket = google_storage_bucket.fn_source.name
        object = google_storage_bucket_object.killswitch.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    available_memory      = "256M"
    timeout_seconds       = 60
    service_account_email = google_service_account.killswitch.email
    environment_variables = { PROJECT_ID = var.project_id }
  }

  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = google_pubsub_topic.budget.id
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.killswitch.email
  }

  depends_on = [
    google_project_iam_member.fn_build,
    google_project_iam_member.killswitch_invoker,
  ]
}

output "killswitch_test" {
  value = "gcloud pubsub topics publish ${google_pubsub_topic.budget.name} --project ${var.project_id} --message='{\"costAmount\": 2, \"budgetAmount\": 1}'"
}
