# Nextflow worker VM. Off by default; start with -var worker_running=true. See docs/gcp-migration.md.

resource "google_project_service" "worker_apis" {
  for_each = toset([
    "compute.googleapis.com",
    "iap.googleapis.com",
    "oslogin.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# Own network, so the setup does not depend on the project's default network
resource "google_compute_network" "net" {
  name                    = "agentic16s-net"
  auto_create_subnetworks = true
  depends_on              = [google_project_service.worker_apis]
}

# SSH only through the IAP tunnel; port 22 is not open to the internet
resource "google_compute_firewall" "iap_ssh" {
  name          = "agentic16s-allow-iap-ssh"
  network       = google_compute_network.net.name
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["agentic16s-worker"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# The VM's own identity. No project roles: the worker only downloads public data and containers.
resource "google_service_account" "worker" {
  account_id   = "agentic16s-worker"
  display_name = "Nextflow worker VM"
  depends_on   = [google_project_service.worker_apis]
}

resource "google_compute_instance" "worker" {
  name           = "agentic16s-worker"
  machine_type   = var.machine_type
  zone           = var.zone
  tags           = ["agentic16s-worker"]
  desired_status = var.worker_running ? "RUNNING" : "TERMINATED"

  # Standard (on-demand) VM: credits do not pay for Spot
  scheduling {
    provisioning_model  = "STANDARD"
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = var.boot_disk_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = google_compute_network.net.id
    access_config {} # ephemeral public IP for ENA, GitHub and container downloads; no Cloud NAT
  }

  service_account {
    email  = google_service_account.worker.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin = "TRUE"
    startup-script = file("${path.module}/startup.sh")
  }

  allow_stopping_for_update = true
  depends_on                = [google_compute_firewall.iap_ssh]
}

output "worker_ssh" {
  value = "infra/gcloud16s compute ssh ${google_compute_instance.worker.name} --zone ${var.zone} --tunnel-through-iap"
}
