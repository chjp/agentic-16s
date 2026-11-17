# Google Batch: one Batch job per study. The job's VM runs the same Docker path as the GCE worker
# (run_study.py --from preflight --no-db) from a fixed code snapshot, uploads the results to the
# bucket below and is deleted. Jobs are submitted from the Mac with infra/batch_submit.py.
# Why one job per study and not Nextflow's per-task google-batch executor: docs/gcp-migration.md section 8.

resource "google_project_service" "batch_apis" {
  for_each = toset([
    "batch.googleapis.com",
    "logging.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# Code snapshots, per-job inputs (manifest) and outputs (tables, reports, logs). No FASTQ: the job
# deletes it on the VM after the integrity checks. Standard class only (credits don't cover
# Coldline/Archive); objects are deleted after 30 days because the Mac keeps the durable copy.
resource "google_storage_bucket" "batch" {
  name                        = "${var.project_id}-batch"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.batch_apis]
}

# Identity of the job VMs. Project roles: report to Batch and write logs. Bucket: read, and create
# new objects (each job writes under its own prefix, so it never needs to overwrite or delete).
resource "google_service_account" "batch" {
  account_id   = "agentic16s-batch"
  display_name = "Google Batch job VMs (one job per study)"
  depends_on   = [google_project_service.batch_apis]
}

resource "google_project_iam_member" "batch_agent" {
  project = var.project_id
  role    = "roles/batch.agentReporter"
  member  = "serviceAccount:${google_service_account.batch.email}"
}

resource "google_project_iam_member" "batch_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.batch.email}"
}

resource "google_storage_bucket_iam_member" "batch_read" {
  bucket = google_storage_bucket.batch.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.batch.email}"
}

resource "google_storage_bucket_iam_member" "batch_create" {
  bucket = google_storage_bucket.batch.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.batch.email}"
}

# Job VMs use the worker's VPC (auto-mode subnet in var.region) with an ephemeral external IP for
# ENA, NCBI, GitHub and container downloads. No firewall rule is needed (no inbound traffic) and no
# Cloud NAT (~$30/month).
output "batch_bucket" {
  value = google_storage_bucket.batch.name
}

output "batch_service_account" {
  value = google_service_account.batch.email
}

output "batch_network" {
  value = google_compute_network.net.id
}

output "batch_subnetwork" {
  value = "projects/${var.project_id}/regions/${var.region}/subnetworks/${google_compute_network.net.name}"
}

output "batch_region" {
  value = var.region
}
