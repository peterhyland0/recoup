output "url" {
  value = "${local.https ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE repository variable in GitHub (Settings > Variables)"
  value       = aws_iam_role.deploy.arn
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.repo : k => r.repository_url }
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "task_network" {
  description = "Subnets and security group for the one-off bootstrap task"
  value = {
    subnets         = aws_subnet.public[*].id
    security_groups = [aws_security_group.tasks.id]
  }
}
