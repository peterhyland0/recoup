variable "region" {
  type    = string
  default = "us-east-1"
}

variable "env" {
  type    = string
  default = "demo"
}

variable "github_repo" {
  description = "owner/name allowed to deploy through GitHub OIDC"
  type        = string
  default     = "peterhyland0/recoup"
}

variable "image_tag" {
  description = "Image tag the services run; the deploy workflow passes the commit SHA"
  type        = string
  default     = "latest"
}

variable "budget_email" {
  description = "Where the monthly budget alarm goes"
  type        = string
}

variable "monthly_budget_usd" {
  description = "Alarm threshold. The running estimate is about $73, so the default leaves headroom"
  type        = number
  default     = 90
}

variable "create_github_oidc_provider" {
  description = "false when the account already has the GitHub OIDC provider (there can be only one)"
  type        = bool
  default     = true
}

variable "fargate_spot" {
  description = "Run the worker and mocks on Fargate Spot (about 70% cheaper; interruptions are fine for them)"
  type        = bool
  default     = true
}

variable "running" {
  description = "false scales every service to zero between demos; the database and load balancer stay"
  type        = bool
  default     = true
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS; empty serves plain HTTP on the load balancer's DNS name"
  type        = string
  default     = ""
}
