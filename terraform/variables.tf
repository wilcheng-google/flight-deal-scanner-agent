variable "project_id" {
  description = "Google Cloud Project ID hosting Vertex AI, Cloud Run, Cloud SQL, and Cloud Scheduler."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region."
  type        = string
  default     = "us-central1"
}

variable "container_image" {
  description = "Artifact Registry container URI for the Flight Deal Scanner Agent."
  type        = string
}

variable "scan_cron_schedule" {
  description = "Cron schedule executing every 2 hours, every day."
  type        = string
  default     = "0 */2 * * *"
}

variable "target_lead_days" {
  description = "Target outbound departure offset in days from current date (~45 days)."
  type        = number
  default     = 45
}

variable "min_trip_duration_days" {
  description = "Minimum trip duration in days (at least 1 week = 7 days)."
  type        = number
  default     = 7
}
