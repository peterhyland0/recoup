locals {
  services = {
    web = {
      image   = "web"
      port    = 3000
      cpu     = 256
      memory  = 512
      command = null
      spot    = false
      env     = {}
    }
    api = {
      image   = "app"
      port    = 8000
      cpu     = 256
      memory  = 512
      command = ["recoup", "serve", "--host", "0.0.0.0", "--port", "8000"]
      spot    = false
      env     = { RECOUP_ROOT_PATH = "/api", RECOUP_MOCKS_URL = "http://mocks.recoup.local:8001" }
    }
    worker = {
      image   = "app"
      port    = null
      cpu     = 256
      memory  = 512
      command = ["recoup", "worker"]
      spot    = var.fargate_spot
      env     = { RECOUP_MOCKS_URL = "http://mocks.recoup.local:8001" }
    }
    mocks = {
      image   = "app"
      port    = 8001
      cpu     = 256
      memory  = 512
      command = ["recoup", "mocks", "--host", "0.0.0.0", "--port", "8001", "--callback", "http://api.recoup.local:8000/api/webhooks/payer"]
      spot    = var.fargate_spot
      env     = {}
    }
  }
  secret_names = ["DATABASE_URL", "RECOUP_WEBHOOK_SECRET", "RECOUP_API_KEYS", "RECOUP_PMS_CLIENT"]
}

resource "aws_ecr_repository" "repo" {
  for_each             = toset(["app", "web"])
  name                 = "recoup-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 15 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 15 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "main" {
  name = "recoup-${var.env}"
  setting {
    name  = "containerInsights"
    value = "disabled" # about $3/month per service when on; CloudWatch logs are enough here
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_cloudwatch_log_group" "service" {
  for_each          = local.services
  name              = "/recoup/${var.env}/${each.key}"
  retention_in_days = 14
}

resource "aws_service_discovery_private_dns_namespace" "internal" {
  name = "recoup.local"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "service" {
  for_each = { for name, svc in local.services : name => svc if svc.port != null }
  name     = each.key
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
}

resource "aws_ecs_task_definition" "service" {
  for_each                 = local.services
  family                   = "recoup-${var.env}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64" # Graviton: about 20% cheaper than x86 on Fargate
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name         = each.key
    image        = "${aws_ecr_repository.repo[each.value.image].repository_url}:${var.image_tag}"
    essential    = true
    command      = each.value.command
    portMappings = each.value.port == null ? [] : [{ containerPort = each.value.port, protocol = "tcp" }]
    environment  = [for k, v in merge(each.value.env, { OTEL_SERVICE_NAME = "recoup-${each.key}" }) : { name = k, value = v }]
    secrets = each.value.image == "app" ? [
      for name in local.secret_names : { name = name, valueFrom = aws_ssm_parameter.secret[name].arn }
    ] : []
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_ecs_service" "service" {
  for_each        = local.services
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service[each.key].arn
  desired_count   = var.running ? 1 : 0

  capacity_provider_strategy {
    capacity_provider = each.value.spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true # no NAT gateway: tasks pull images over the internet gateway
  }

  dynamic "load_balancer" {
    for_each = contains(["web", "api"], each.key) ? [each.key] : []
    content {
      target_group_arn = aws_lb_target_group.service[each.key].arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  dynamic "service_registries" {
    for_each = each.value.port == null ? [] : [each.key]
    content {
      registry_arn = aws_service_discovery_service.service[each.key].arn
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition] # the deploy workflow rolls new task definitions
  }

  # Without these, a first apply can race: "target group has no load balancer" or
  # "capacity provider not associated with the cluster".
  depends_on = [aws_lb_listener.http, aws_lb_listener_rule.api, aws_ecs_cluster_capacity_providers.main]
}

# A one-off task the deploy workflow runs before rolling services: migrations as the
# admin role, then the least-privilege app role.
resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "recoup-${var.env}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name      = "bootstrap"
    image     = "${aws_ecr_repository.repo["app"].repository_url}:${var.image_tag}"
    essential = true
    command   = ["recoup", "db-bootstrap", "--seed", "datasets/full"]
    secrets = [
      { name = "DATABASE_URL", valueFrom = aws_ssm_parameter.secret["ADMIN_DATABASE_URL"].arn },
      { name = "APP_DB_PASSWORD", valueFrom = aws_ssm_parameter.secret["APP_DB_PASSWORD"].arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service["api"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "bootstrap"
      }
    }
  }])
}
