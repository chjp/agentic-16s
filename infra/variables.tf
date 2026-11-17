variable "project_id" {
  type = string
}

variable "billing_account" {
  type        = string
  description = "Cloud Billing account ID, like 0X0X0X-0X0X0X-0X0X0X"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "card_cap_usd" {
  type        = number
  default     = 1
  description = "Net cost after all credits (what reaches the card) at which billing is unlinked from the project"
}

variable "machine_type" {
  type        = string
  default     = "e2-standard-2" # smoke tests; full runs: -var machine_type=e2-standard-8 (free trial caps at 8 vCPUs)
  description = "Worker machine type. General-purpose e2/n2d only: Spot and some compute/memory-optimized types are excluded from credits"
}

variable "boot_disk_gb" {
  type    = number
  default = 50
}

variable "worker_running" {
  type        = bool
  default     = false
  description = "true starts the worker, false stops it (the VM and its disk stay)"
}
