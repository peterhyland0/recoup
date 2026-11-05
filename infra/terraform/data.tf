resource "random_password" "db_admin" {
  length  = 32
  special = false
}

resource "random_password" "db_app" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "recoup-${var.env}"
  subnet_ids = aws_subnet.isolated[*].id
}

resource "aws_db_instance" "main" {
  identifier                   = "recoup-${var.env}"
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = "db.t4g.micro"
  allocated_storage            = 20
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = "recoup"
  username                     = "recoup_admin"
  password                     = random_password.db_admin.result
  db_subnet_group_name         = aws_db_subnet_group.main.name
  vpc_security_group_ids       = [aws_security_group.db.id]
  publicly_accessible          = false
  backup_retention_period      = 7
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "recoup-${var.env}-final"
  performance_insights_enabled = false
}

# Secrets the tasks read at start. The app connects as recoup_app, a role with neither
# SUPERUSER nor BYPASSRLS, because either silently switches row-level security off.
# `recoup db-bootstrap` (run by the deploy workflow as a one-off task) creates that role.
locals {
  db_host = aws_db_instance.main.address
  parameters = {
    DATABASE_URL          = "postgresql+psycopg://recoup_app:${random_password.db_app.result}@${local.db_host}:5432/recoup"
    ADMIN_DATABASE_URL    = "postgresql+psycopg://recoup_admin:${random_password.db_admin.result}@${local.db_host}:5432/recoup"
    APP_DB_PASSWORD       = random_password.db_app.result
    RECOUP_WEBHOOK_SECRET = random_password.webhook.result
    RECOUP_API_KEYS       = "${random_password.demo_key.result}:demo:demo-biller,${random_password.service_key.result}:demo"
    RECOUP_PMS_CLIENT     = "recoup:${random_password.pms.result}"
  }
}

resource "random_password" "webhook" {
  length  = 40
  special = false
}

resource "random_password" "demo_key" {
  length  = 32
  special = false
}

resource "random_password" "service_key" {
  length  = 32
  special = false
}

resource "random_password" "pms" {
  length  = 32
  special = false
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.parameters
  name     = "/recoup/${var.env}/${each.key}"
  type     = "SecureString"
  value    = each.value
}
