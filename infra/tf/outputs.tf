output "control_plane_public_ip" {
  description = "Public IP of the control-plane EC2 instance"
  value       = module.k8s_cluster.control_plane_public_ip
}

output "vpc_id" {
  description = "ID of the cluster VPC"
  value       = module.vpc.vpc_id
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group"
  value       = module.k8s_cluster.worker_asg_name
}

output "alb_dns_name" {
  description = "DNS name of the ALB fronting the cluster"
  value       = module.ingress.alb_dns_name
}

output "domain_name" {
  description = "Base domain Ingress hosts live under (*.<domain_name>)"
  value       = module.ingress.domain_name
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic Alertmanager publishes alerts to - fed into the kube-prometheus-stack Helm install as --set overrides (infra/k8s/bootstrap.sh)"
  value       = module.monitoring.sns_topic_arn
}
