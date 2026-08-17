output "alb_dns_name" {
  description = "DNS name of the ALB fronting the cluster"
  value       = aws_lb.this.dns_name
}

output "domain_name" {
  description = "Base domain Ingress hosts live under (*.<domain_name> resolves to the ALB)"
  value       = var.domain_name
}

output "target_group_arn" {
  description = "ARN of the target group workers are attached to"
  value       = aws_lb_target_group.http.arn
}
