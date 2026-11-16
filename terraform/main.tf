terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.12"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Dedicated least-privilege Service Account for the Flight Deal Scanner Agent
resource "google_service_account" "scanner_agent_sa" {
  account_id   = "flight-scanner-agent-sa"
  display_name = "Flight Deal Scanner ADK Agent Runtime Identity"
}

locals {
  agent_roles = [
    "roles/aiplatform.user",
    "roles/cloudsql.client",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/dlp.user",
    "roles/secretmanager.secretAccessor",
  ]
}

resource "google_project_iam_member" "scanner_agent_iam" {
  for_each = toset(local.agent_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.scanner_agent_sa.email}"
}

# Secret Manager containers for external flight provider API keys (no hardcoded keys)
resource "google_secret_manager_secret" "serpapi_key" {
  secret_id = "SERPAPI_API_KEY"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "trip_com_key" {
  secret_id = "TRIP_COM_PARTNER_KEY"
  replication {
    auto {}
  }
}

# Persistent Cloud SQL PostgreSQL instance for ADK DatabaseSessionService & 30d fare history
resource "google_sql_database_instance" "scanner_postgres" {
  name                = "flight-scanner-state-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier = "db-custom-1-3840"
    backup_configuration {
      enabled = true
    }
  }
}

resource "google_sql_database" "scanner_db" {
  name     = "flight_scanner"
  instance = google_sql_database_instance.scanner_postgres.name
}

# Cloud Run v2 Service hosting the ADK Flight Deal Scanner API & A2A endpoint
resource "google_cloud_run_v2_service" "flight_scanner_service" {
  name     = "flight-deal-scanner-agent"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.scanner_agent_sa.email

    containers {
      image = var.container_image

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "OTEL_EXPORT_TO_CLOUD"
        value = "true"
      }
      env {
        name  = "ENABLE_GCP_STRUCTURED_LOGGING"
        value = "true"
      }
      env {
        name  = "TARGET_LEAD_DAYS"
        value = tostring(var.target_lead_days)
      }
      env {
        name  = "MIN_TRIP_DURATION_DAYS"
        value = tostring(var.min_trip_duration_days)
      }

      env {
        name = "SERPAPI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.serpapi_key.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "TRIP_COM_PARTNER_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.trip_com_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }
}

# Dedicated Cloud Scheduler Service Account to invoke the scan every 2 hours
resource "google_service_account" "scheduler_invoker_sa" {
  account_id   = "flight-scanner-cron-sa"
  display_name = "Cloud Scheduler 2-Hour Flight Scan Invoker"
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker_binding" {
  name     = google_cloud_run_v2_service.flight_scanner_service.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker_sa.email}"
}

# Automated Every-2-Hours (12x Daily) Airfare Price-Drop Check
resource "google_cloud_scheduler_job" "every_two_hours_flight_scan" {
  name        = "flight-deal-scanner-every-2h"
  description = "Triggers SNA & LAX -> Japan/London/Vancouver/Toronto/Spain/Italy fare scan every 2 hours."
  schedule    = var.scan_cron_schedule
  time_zone   = "America/Los_Angeles"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.flight_scanner_service.uri}/scan/scheduled"

    oidc_token {
      service_account_email = google_service_account.scheduler_invoker_sa.email
    }
  }
}
