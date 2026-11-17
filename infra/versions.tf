terraform {
  required_version = ">= 1.9"
  required_providers {
    google  = { source = "hashicorp/google", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.0" } # zips the kill switch source
  }
  # Local state for now. Move to a GCS backend once the worker exists (docs/gcp-migration.md 5.1).
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # With local user credentials (ADC), bill API quota to this project. Without it the Budget API
  # and the Eventarc trigger check are charged to gcloud's own client project and fail with 403.
  user_project_override = true
  billing_project       = var.project_id
}
