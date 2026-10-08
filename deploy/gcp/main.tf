terraform {
  required_version = ">= 1.12, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0, < 8.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

variable "project_id" { type = string }
variable "region" { type = string }
variable "zone" { type = string }
variable "name" {
  type    = string
  default = "knowledge-layer"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "Use a short lowercase deployment name."
  }
}
variable "machine_type" {
  type    = string
  default = "e2-medium"
}
variable "data_disk_gb" {
  type    = number
  default = 30
  validation {
    condition     = var.data_disk_gb >= 30 && var.data_disk_gb <= 1024
    error_message = "Choose a data disk from 30 to 1024 GiB."
  }
}
variable "https_source_ranges" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

resource "google_compute_network" "pilot" {
  name                    = var.name
  auto_create_subnetworks = false
}
resource "google_compute_subnetwork" "pilot" {
  name          = var.name
  network       = google_compute_network.pilot.id
  region        = var.region
  ip_cidr_range = "10.74.0.0/24"
}
resource "google_compute_firewall" "https" {
  name          = "${var.name}-https"
  network       = google_compute_network.pilot.id
  target_tags   = [var.name]
  source_ranges = var.https_source_ranges
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}
resource "google_compute_firewall" "iap" {
  name          = "${var.name}-iap-ssh"
  network       = google_compute_network.pilot.id
  target_tags   = [var.name]
  source_ranges = ["35.235.240.0/20"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
resource "google_compute_address" "pilot" {
  name   = var.name
  region = var.region
}
resource "google_service_account" "pilot" {
  account_id   = var.name
  display_name = "Knowledge Layer pilot - no project roles"
}
resource "google_compute_resource_policy" "backup" {
  name   = "${var.name}-daily-disk-snapshot"
  region = var.region
  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "04:00"
      }
    }
    retention_policy {
      max_retention_days    = 7
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      storage_locations = [var.region]
    }
  }
}
resource "google_compute_disk" "data" {
  name              = "${var.name}-data"
  zone              = var.zone
  type              = "pd-balanced"
  size              = var.data_disk_gb
  labels            = { application = "knowledge-layer", role = "registry" }
  lifecycle { prevent_destroy = true }
}
resource "google_compute_disk_resource_policy_attachment" "backup" {
  name = google_compute_resource_policy.backup.name
  disk = google_compute_disk.data.name
  zone = var.zone
}
resource "google_compute_instance" "pilot" {
  name                = var.name
  machine_type        = var.machine_type
  zone                = var.zone
  deletion_protection = true
  tags                = [var.name]
  labels              = { application = "knowledge-layer", qualification = "pilot" }
  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 20
      type  = "pd-balanced"
    }
  }
  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "knowledge-data"
  }
  network_interface {
    subnetwork = google_compute_subnetwork.pilot.id
    access_config { nat_ip = google_compute_address.pilot.address }
  }
  service_account {
    email  = google_service_account.pilot.email
    scopes = ["cloud-platform"]
  }
  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
  }
  metadata_startup_script = file("${path.module}/bootstrap.sh")
  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }
}

output "public_ip" { value = google_compute_address.pilot.address }
output "instance_name" { value = google_compute_instance.pilot.name }
output "zone" { value = var.zone }
output "project_id" { value = var.project_id }
